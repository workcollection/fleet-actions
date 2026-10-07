#!/bin/bash
# mari-gate: cap and load-gate a fixed pool of long-lived docker runner containers
# (<RUNNER_PREFIX>1 .. <RUNNER_PREFIX><POOL_SIZE>) that shares its host with other workloads.
#
# A long-lived runner starts a job whenever GitHub assigns one to it while it is idle and
# online. So the only way to stop new jobs is to take idle runners offline:
#   - host load1 >= LOAD_MAX: park every IDLE runner. Busy runners finish their job.
#   - otherwise: park idle runners above CAP, and unpark parked runners up to CAP, but only
#     HOLD_S after the last park, so a load spike does not flap runners on and off.
# Park   = mark parked, `docker stop -t 30` (the runner entrypoint deregisters on SIGTERM), rm.
# Unpark = LAUNCH_CMD <index> (your launcher: fresh registration and env), then unmark.
# A relauncher that restarts "missing" runners must skip the indices in $STATE/parked/.
#
# Accepted race: a job that GitHub assigns in the second between the idle check and the
# stop fails and needs a rerun.
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
# A runner is busy while a Runner.Worker process (one job) is alive inside its container.
busy(){ local out; out=$(docker top "$(name "$1")" -eo pid,comm 2>/dev/null); [[ $out == *Runner.Worker* ]]; }
others(){ [ -n "$OTHER_JOB_PREFIX" ] || { echo 0; return; }
  local out; out=$(docker ps --format '{{.Names}}'); awk -v p="$OTHER_JOB_PREFIX" 'index($0, p) == 1 { n++ } END { print n + 0 }' <<<"$out"; }
park(){
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
  for i in "${idle[@]}"; do park "$i" "load1 $load >= $LOAD_MAX"; done
  exit 0
fi
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
