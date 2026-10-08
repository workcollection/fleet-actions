#!/bin/bash
# mari-gate: cap and load-gate a fixed pool of long-lived docker runner containers
# (<RUNNER_PREFIX>1 .. <RUNNER_PREFIX><POOL_SIZE>) that shares its host with other workloads.
#
# A long-lived runner starts a job whenever GitHub assigns one to it while it is idle and
# online. So the only way to stop new jobs is to take idle runners offline:
#   - host load1 >= LOAD_MAX on HIGH_TICKS consecutive ticks: park every IDLE runner. Busy runners
#     finish their job. A shorter spike only waits (no park, no unpark): parking is the one action
#     that can kill a job being assigned, so it should not react to a single sample.
#     LOAD_PARK=0: high load only HOLDS (no park, no unpark). Parking an ONLINE runner can kill a job
#     that GitHub assigns during the graceful stop, and GitHub's assignment lag makes that impossible
#     to rule out (measured: every zero-step job loss coincided with a load park).
#   - otherwise: park idle runners above CAP, and unpark parked runners up to CAP, but only
#     HOLD_S after the last park, so a load spike does not flap runners on and off.
# Park   = mark parked, `docker stop -t 30` (the runner entrypoint deregisters on SIGTERM), rm.
# Unpark = LAUNCH_CMD <index> (your launcher: fresh registration and env), then unmark.
# A relauncher that restarts "missing" runners must skip the indices in $STATE/parked/.
#
# Race: a job that GitHub assigns between the idle check and the stop fails with no step run
# (10 min later, 'lost communication'). park() therefore re-checks right before each stop, and a
# runner counts as busy as soon as it logged 'Running job:' (before Runner.Worker exists). What
# remains is the gap between that re-check and the end of the graceful stop. Under a job backlog a
# runner that finishes a job gets the next one within 1-2 s, and the listener keeps accepting while
# the stop deregisters it (several seconds): MIN_IDLE_S > 0 parks only a runner that has been idle
# that long (since its last 'completed with result' / 'Listening for Jobs'), i.e. one GitHub is not
# about to hand a job.
# MODE=observe only logs what enforce would do.
# A failed unpark logs the launcher's last output line and counts in $STATE/unpark_fail (reset by
# any successful unpark; shown as unpark_fail= in the status line). At FAIL_ALERT consecutive failures,
# and every 12*FAIL_ALERT after that, ALERT_CMD (optional) runs with one message argument: a launcher
# that can never succeed (missing image, bad token) must not look like an intentionally parked pool.
# OTHER_JOB_PREFIX (optional): running containers whose name starts with it are jobs of another
# runner pool on the same host (one container = one job). They count against CAP, so CAP means
# "jobs on this host": this pool gets CAP minus their number. The gate never stops them.
set -uo pipefail
RUNNER_PREFIX=runner-
POOL_SIZE=10
CAP=6
LOAD_MAX=8
HOLD_S=120
LAUNCH_CMD=/usr/local/sbin/run-runners.sh
STATE=/var/lib/mari-gate
LOADAVG=/proc/loadavg          # tests point this at a fixture
MODE=enforce
OTHER_JOB_PREFIX=
FAIL_ALERT=10
HIGH_TICKS=1
MIN_IDLE_S=0
LOAD_PARK=1
ALERT_CMD=
CONF=${MARI_GATE_CONF:-/etc/mari-gate.conf}
[ -f "$CONF" ] && . "$CONF"
mkdir -p "$STATE/parked"
# One journal line per decision: under systemd (INVOCATION_ID set) stdout already goes to the
# journal, so only print when run by hand.
log(){ logger -t mari-gate "$*"; [ -n "${INVOCATION_ID:-}" ] || echo "mari-gate: $*"; }
name(){ echo "${RUNNER_PREFIX}$1"; }
# Capture first, then match: with pipefail, `docker ... | grep -q` fails when grep exits
# early and docker gets SIGPIPE, which would report a busy runner as idle and park it mid-job.
running(){ local out; out=$(docker ps --format '{{.Names}}'); grep -qx "$(name "$1")" <<<"$out"; }
# A runner is busy while a Runner.Worker process (one job) is alive inside its container, or once
# its listener logged 'Running job:' with no later 'completed with result' (the job is assigned, the
# worker not started yet).
busy(){ local out; out=$(docker top "$(name "$1")" -eo pid,comm 2>/dev/null); [[ $out == *Runner.Worker* ]] && return 0
  out=$(docker logs --tail 40 "$(name "$1")" 2>&1); out=$(grep -E 'Running job:|completed with result' <<<"$out" | tail -1)
  [[ $out == *"Running job:"* ]]; }
others(){ [ -n "$OTHER_JOB_PREFIX" ] || { echo 0; return; }
  local out; out=$(docker ps --format '{{.Names}}'); awk -v p="$OTHER_JOB_PREFIX" 'index($0, p) == 1 { n++ } END { print n + 0 }' <<<"$out"; }
# Seconds since the runner last became idle (its newest 'completed with result' or 'Listening for
# Jobs' line, by the runner's own timestamp); a large number when there is no such line.
idle_s(){ local out t; out=$(docker logs --tail 40 "$(name "$1")" 2>&1)
  t=$(grep -E 'completed with result|Listening for Jobs' <<<"$out" | tail -1 | grep -oE '^[0-9]{4}-[0-9]{2}-[0-9]{2} [0-9:]{8}Z')
  [ -n "$t" ] && t=$(date -u -d "$t" +%s 2>/dev/null); [ -n "$t" ] || { echo 99999; return; }
  echo $(( $(date +%s) - t )); }
park(){
  # The idle list is built once per tick and each stop takes seconds: look again right before this one.
  if busy "$1"; then log "skip park $(name "$1"): took a job since the idle check"; return 0; fi
  if [ "$MIN_IDLE_S" -gt 0 ]; then local s; s=$(idle_s "$1")
    if [ "$s" -lt "$MIN_IDLE_S" ]; then log "skip park $(name "$1"): idle only ${s}s (< MIN_IDLE_S $MIN_IDLE_S), a job may be on its way"; return 0; fi
  fi
  log "park $(name "$1") ($2) [$MODE]"
  [ "$MODE" = enforce ] || return 0
  touch "$STATE/parked/$1"; date +%s > "$STATE/last_park"
  docker stop -t 30 "$(name "$1")" >/dev/null 2>&1; docker rm -f "$(name "$1")" >/dev/null 2>&1
}
unpark(){
  log "unpark $(name "$1") ($2) [$MODE]"
  [ "$MODE" = enforce ] || return 0
  local out n
  if out=$(bash "$LAUNCH_CMD" "$1" 2>&1); then
    rm -f "$STATE/parked/$1" "$STATE/unpark_fail"
  else
    out=$(printf '%s\n' "$out" | grep -v '^[[:space:]]*$' | tail -1)
    n=$(( $(cat "$STATE/unpark_fail" 2>/dev/null || echo 0) + 1 )); echo "$n" > "$STATE/unpark_fail"
    log "unpark $(name "$1") FAILED ($n in a row): ${out:-no output}"
    if [ -n "$ALERT_CMD" ] && { [ "$n" -eq "$FAIL_ALERT" ] || [ $(( n % (FAIL_ALERT * 12) )) -eq 0 ]; }; then
      $ALERT_CMD "mari-gate: $n unparks in a row failed; last: $(name "$1"): ${out:-no output}" >/dev/null 2>&1 || log "ALERT_CMD failed"
    fi
  fi
}

load=$(cut -d' ' -f1 "$LOADAVG")
high=$(awk -v l="$load" -v m="$LOAD_MAX" 'BEGIN{print (l >= m) ? 1 : 0}')
nother=$(others)
limit=$(( CAP - nother )); [ "$limit" -lt 0 ] && limit=0
run=(); idle=(); nbusy=0
for i in $(seq 1 "$POOL_SIZE"); do
  running "$i" || continue
  run+=("$i")
  if busy "$i"; then nbusy=$((nbusy+1)); else idle+=("$i"); fi
done
echo "$(date -u +%FT%TZ) load1=$load running=${#run[@]} busy=$nbusy idle=${#idle[@]} other=$nother cap=$CAP limit=$limit unpark_fail=$(cat "$STATE/unpark_fail" 2>/dev/null || echo 0) mode=$MODE" > "$STATE/status"

if [ "$high" = 1 ]; then
  ht=$(( $(cat "$STATE/high_ticks" 2>/dev/null || echo 0) + 1 )); echo "$ht" > "$STATE/high_ticks"
  if [ "$ht" -lt "$HIGH_TICKS" ]; then log "load1 $load >= $LOAD_MAX (tick $ht/$HIGH_TICKS): waiting, no park, no unpark"; exit 0; fi
  if [ "$LOAD_PARK" = 0 ]; then
    [ "$ht" -eq "$HIGH_TICKS" ] && log "load1 $load >= $LOAD_MAX: holding (LOAD_PARK=0: no park, no unpark)"
    exit 0
  fi
  for i in "${idle[@]}"; do park "$i" "load1 $load >= $LOAD_MAX, $ht ticks"; done
  exit 0
fi
rm -f "$STATE/high_ticks"
# Over the limit (CAP minus other jobs): park idle runners, highest index first.
over=$(( ${#run[@]} - limit ))
if [ "$over" -gt 0 ]; then
  for ((k=${#idle[@]}-1; k>=0 && over>0; k--)); do park "${idle[$k]}" "cap $CAP, other jobs $nother"; over=$((over-1)); done
  exit 0
fi
# Under the cap: unpark, lowest index first, after the hold time.
last=$(cat "$STATE/last_park" 2>/dev/null || echo 0)
[ $(( $(date +%s) - last )) -lt "$HOLD_S" ] && exit 0
need=$(( limit - ${#run[@]} ))
for i in $(seq 1 "$POOL_SIZE"); do
  [ "$need" -gt 0 ] || break
  running "$i" && continue
  unpark "$i" "load1 $load < $LOAD_MAX, ${#run[@]}/$limit running (cap $CAP, other jobs $nother)"; need=$((need-1))
done
