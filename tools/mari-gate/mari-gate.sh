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
#     Emergency valve (EMERGENCY_LOAD > 0): with LOAD_PARK=0, idle runners ARE parked when load1 >=
#     EMERGENCY_LOAD on EMERGENCY_TICKS consecutive ticks AND CPU pressure 'some avg10' (PSI_FILE, at
#     most PSI_MAX_AGE s old) >= EMERGENCY_PSI %. A real overload that starves other tenants outweighs
#     the small risk to a job. Logged as 'EMERGENCY' and counted in $STATE/emergency_parks.
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
#
# Throttle ladder (LADDER=observe|enforce; 0 = off): slow CI down before stopping anything, keyed on
# the CPU pressure of the tenants CI must not hurt (LADDER_PSI_FILE, 'some avg10=<max of them>',
# written by the host). Levels, each entered after LADDER_Nk ticks in a row at >= LADDER_Tk %:
#   L1: HOLD - no unpark. Running jobs are untouched.
#   L2: LADDER_CGROUP/cpu.max = LADDER_CPU2 (the cgroup that holds dockerd, BuildKit and every
#       container, so job-spawned containers and image builds are covered too).
#   L3: cpu.max = LADDER_CPU3; after LADDER_PARK_AFTER_S at L3, park one IDLE runner per tick.
#   A clamp is never below 2 CPUs: a starved listener could look offline and lose a job.
# Step down one level after LADDER_CALM ticks below the current level's threshold. cpu.max is saved
# when L2 is entered and written back exactly when the ladder drops below L2.
# A missing input, or one older than LADDER_STALE_S, never escalates: the level holds, and after
# LADDER_STALE_RESTORE_S of stale input the ladder goes back to L0.
# Every tick at L2/L3 re-writes the level's value (a reboot resets the cgroup); LADDER=0 with a
# saved value restores it. `mari-gate.sh --restore` restores and resets the ladder (run it before
# stopping the timer). observe tracks and logs the levels, and writes, holds and parks nothing.
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
EMERGENCY_LOAD=0
EMERGENCY_TICKS=10
EMERGENCY_PSI=30
PSI_FILE=/proc/pressure/cpu
PSI_MAX_AGE=120
ALERT_CMD=
LADDER=0
LADDER_PSI_FILE=/run/mari-gate/wing-cpu-pressure
LADDER_CGROUP=/sys/fs/cgroup/system.slice
LADDER_T1=10; LADDER_T2=15; LADDER_T3=25
LADDER_N1=3;  LADDER_N2=5;  LADDER_N3=10
LADDER_CALM=5
LADDER_CPU2="400000 100000"
LADDER_CPU3="200000 100000"
LADDER_STALE_S=90
LADDER_STALE_RESTORE_S=600
LADDER_PARK_AFTER_S=900
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
  if busy "$1"; then log "skip park $(name "$1"): took a job since the idle check"; return 1; fi
  if [ "$MIN_IDLE_S" -gt 0 ]; then local s; s=$(idle_s "$1")
    if [ "$s" -lt "$MIN_IDLE_S" ]; then log "skip park $(name "$1"): idle only ${s}s (< MIN_IDLE_S $MIN_IDLE_S), a job may be on its way"; return 1; fi
  fi
  log "park $(name "$1") ($2) [$MODE]"
  [ "$MODE" = enforce ] || return 0
  touch "$STATE/parked/$1"; date +%s > "$STATE/last_park"
  docker stop -t 30 "$(name "$1")" >/dev/null 2>&1; docker rm -f "$(name "$1")" >/dev/null 2>&1
}
unpark(){
  log "unpark $(name "$1") ($2) [$MODE]"
  [ "$MODE" = enforce ] || return 0
  local out n raw
  if out=$(bash "$LAUNCH_CMD" "$1" 2>&1); then
    rm -f "$STATE/parked/$1" "$STATE/unpark_fail"
  else
    raw=$out
    # The line that names the error ('docker: Error response …: Conflict …'), not docker's trailing
    # "Run 'docker run --help' for more information"; else the last non-empty line.
    out=$(printf '%s\n' "$out" | grep -v '^[[:space:]]*$' | grep -v -- "--help' for more information" \
      | { grep -i -m1 -E 'error|denied|conflict|failed|not found|no such' || true; } | head -1) \
      || true
    [ -n "$out" ] || out=$(printf '%s\n' "$raw" | grep -v '^[[:space:]]*$' | tail -1)
    n=$(( $(cat "$STATE/unpark_fail" 2>/dev/null || echo 0) + 1 )); echo "$n" > "$STATE/unpark_fail"
    log "unpark $(name "$1") FAILED ($n in a row): ${out:-no output}"
    if [ -n "$ALERT_CMD" ] && { [ "$n" -eq "$FAIL_ALERT" ] || [ $(( n % (FAIL_ALERT * 12) )) -eq 0 ]; }; then
      $ALERT_CMD "mari-gate: $n unparks in a row failed; last: $(name "$1"): ${out:-no output}" >/dev/null 2>&1 || log "ALERT_CMD failed"
    fi
  fi
}

# --- throttle ladder ---
ladder_floor(){ # "quota period" raised to at least 2 CPUs; "max ..." unchanged
  local q p; read -r q p <<<"$1"
  [ "$q" = max ] || { [ "$q" -lt $(( 2 * p )) ] && q=$(( 2 * p )); }; echo "$q $p"; }
ladder_cpu(){ case "$1" in 2) ladder_floor "$LADDER_CPU2" ;; 3) ladder_floor "$LADDER_CPU3" ;; esac; }
# Write cpu.max for level $1 (enforce only). <2 = the saved value (then forget it); >=2 = the level's clamp.
ladder_apply(){ local cg="$LADDER_CGROUP/cpu.max" cur want
  [ "$LADDER" = enforce ] || return 0
  cur=$(cat "$cg" 2>/dev/null) || { log "ladder: cannot read $cg"; return 1; }
  if [ "$1" -lt 2 ]; then
    [ -f "$STATE/ladder_saved" ] || return 0
    want=$(cat "$STATE/ladder_saved")
    if [ "$cur" != "$want" ]; then echo "$want" > "$cg" || { log "ladder: restoring $cg to '$want' FAILED"; return 1; }
      log "ladder: $cg restored to '$want' (was '$cur')"; fi
    rm -f "$STATE/ladder_saved"; return 0
  fi
  [ -f "$STATE/ladder_saved" ] || echo "$cur" > "$STATE/ladder_saved"
  want=$(ladder_cpu "$1")
  if [ "$cur" != "$want" ]; then echo "$want" > "$cg" || { log "ladder: writing '$want' to $cg FAILED"; return 1; }
    log "ladder: $cg = '$want' (was '$cur')"; fi; }
ladder_restore(){ LADDER=enforce ladder_apply 0; rm -f "$STATE"/ladder_level "$STATE"/ladder_ticks "$STATE"/ladder_l3_since "$STATE"/ladder_stale_since; }
# Fresh input value, or nothing (missing, unreadable or older than LADDER_STALE_S).
ladder_psi(){ [ -r "$LADDER_PSI_FILE" ] || return 0
  [ $(( $(date +%s) - $(stat -c %Y "$LADDER_PSI_FILE") )) -le "$LADDER_STALE_S" ] || return 0
  sed -n 's/^some avg10=\([0-9.]*\).*/\1/p' "$LADDER_PSI_FILE" | head -1; }
ge(){ awk -v a="$1" -v b="$2" 'BEGIN{exit !(a >= b)}'; }
# Sets L (level) and wpsi (input, or 'stale'). Moves at most one level per tick.
ladder_tick(){ local t=(0 "$LADDER_T1" "$LADDER_T2" "$LADDER_T3") n=(0 "$LADDER_N1" "$LADDER_N2" "$LADDER_N3") u c k new now
  L=$(cat "$STATE/ladder_level" 2>/dev/null || echo 0); now=$(date +%s)
  wpsi=$(ladder_psi)
  if [ -z "$wpsi" ]; then
    wpsi=stale; rm -f "$STATE/ladder_ticks"
    if [ ! -f "$STATE/ladder_stale_since" ]; then echo "$now" > "$STATE/ladder_stale_since"
      log "ladder: input $LADDER_PSI_FILE missing or older than ${LADDER_STALE_S}s: holding L$L, no escalation"; fi
    if [ "$L" -gt 0 ] && [ $(( now - $(cat "$STATE/ladder_stale_since") )) -ge "$LADDER_STALE_RESTORE_S" ]; then
      log "ladder: input stale for $(( (now - $(cat "$STATE/ladder_stale_since")) / 60 )) min: L$L -> L0 [$LADDER]"; L=0; echo 0 > "$STATE/ladder_level"; rm -f "$STATE/ladder_l3_since"; fi
    ladder_apply "$L"; return 0
  fi
  [ -f "$STATE/ladder_stale_since" ] && { rm -f "$STATE/ladder_stale_since"; log "ladder: input fresh again (${wpsi}%)"; }
  # $STATE/ladder_ticks: "<ticks >= T1> <ticks >= T2> <ticks >= T3> <calm ticks>"
  read -r -a u < "$STATE/ladder_ticks" 2>/dev/null; [ "${#u[@]}" -eq 4 ] || u=(0 0 0 0); u=(0 "${u[@]}")
  for k in 1 2 3; do if ge "$wpsi" "${t[$k]}"; then u[$k]=$(( u[k] + 1 )); else u[$k]=0; fi; done
  c=${u[4]}; new=$L
  if [ "$L" -lt 3 ] && [ "${u[$((L+1))]}" -ge "${n[$((L+1))]}" ]; then new=$(( L + 1 )); c=0
  elif [ "$L" -gt 0 ]; then
    if ge "$wpsi" "${t[$L]}"; then c=0; else c=$(( c + 1 )); fi
    [ "$c" -ge "$LADDER_CALM" ] && { new=$(( L - 1 )); c=0; }
  fi
  u[4]=$c; echo "${u[*]:1}" > "$STATE/ladder_ticks"
  if [ "$new" != "$L" ]; then
    log "ladder: L$L -> L$new (wing CPU pressure ${wpsi}%) [$LADDER]"; L=$new; echo "$L" > "$STATE/ladder_level"
    if [ "$L" = 3 ]; then echo "$now" > "$STATE/ladder_l3_since"; else rm -f "$STATE/ladder_l3_since"; fi
  fi
  ladder_apply "$L"; }

if [ "${1:-}" = --restore ]; then ladder_restore; log "ladder: --restore done (cpu.max $(cat "$LADDER_CGROUP/cpu.max" 2>/dev/null))"; exit 0; fi
L=0; wpsi=off
if [ "$LADDER" = observe ] || [ "$LADDER" = enforce ]; then ladder_tick
elif [ -f "$STATE/ladder_saved" ] || [ -f "$STATE/ladder_level" ]; then log "ladder: LADDER=$LADDER, cleaning up"; ladder_restore; fi

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
echo "$(date -u +%FT%TZ) load1=$load running=${#run[@]} busy=$nbusy idle=${#idle[@]} other=$nother cap=$CAP limit=$limit unpark_fail=$(cat "$STATE/unpark_fail" 2>/dev/null || echo 0) mode=$MODE ladder=L$L/$LADDER wing_psi=$wpsi" > "$STATE/status"

# Ladder L3 held long enough: park one IDLE runner per tick (park() re-checks busy and MIN_IDLE_S).
if [ "$LADDER" = enforce ] && [ "$L" = 3 ] && [ -f "$STATE/ladder_l3_since" ] \
   && [ $(( $(date +%s) - $(cat "$STATE/ladder_l3_since") )) -ge "$LADDER_PARK_AFTER_S" ]; then
  for ((k=${#idle[@]}-1; k>=0; k--)); do
    if park "${idle[$k]}" "ladder L3 for $(( ($(date +%s) - $(cat "$STATE/ladder_l3_since")) / 60 )) min, wing CPU pressure ${wpsi}%"; then
      echo $(( $(cat "$STATE/ladder_parks" 2>/dev/null || echo 0) + 1 )) > "$STATE/ladder_parks"; break; fi
  done
fi

if [ "$high" = 1 ]; then
  ht=$(( $(cat "$STATE/high_ticks" 2>/dev/null || echo 0) + 1 )); echo "$ht" > "$STATE/high_ticks"
  if [ "$ht" -lt "$HIGH_TICKS" ]; then log "load1 $load >= $LOAD_MAX (tick $ht/$HIGH_TICKS): waiting, no park, no unpark"; exit 0; fi
  if [ "$LOAD_PARK" = 0 ]; then
    et=0; [ "$EMERGENCY_LOAD" != 0 ] && awk -v l="$load" -v m="$EMERGENCY_LOAD" 'BEGIN{exit !(l >= m)}' \
      && et=$(( $(cat "$STATE/emergency_ticks" 2>/dev/null || echo 0) + 1 ))
    echo "$et" > "$STATE/emergency_ticks"
    if [ "$et" -ge "$EMERGENCY_TICKS" ]; then
      psi=""
      if [ -r "$PSI_FILE" ] && [ $(( $(date +%s) - $(stat -c %Y "$PSI_FILE") )) -le "$PSI_MAX_AGE" ]; then
        psi=$(sed -n 's/^some avg10=\([0-9.]*\).*/\1/p' "$PSI_FILE")
      fi
      if [ -z "$psi" ]; then
        log "EMERGENCY check: load1 $load >= $EMERGENCY_LOAD for $et ticks, but no fresh CPU pressure in $PSI_FILE: holding"
      elif awk -v p="$psi" -v m="$EMERGENCY_PSI" 'BEGIN{exit !(p >= m)}'; then
        logger -p user.warning -t mari-gate "EMERGENCY: load1 $load >= $EMERGENCY_LOAD for $et ticks and CPU pressure $psi% >= $EMERGENCY_PSI%: parking idle runners"
        [ -n "${INVOCATION_ID:-}" ] || echo "mari-gate: EMERGENCY: load1 $load, $et ticks, CPU pressure $psi%: parking idle runners"
        for i in "${idle[@]}"; do park "$i" "EMERGENCY load1 $load, CPU pressure $psi%" \
          && echo $(( $(cat "$STATE/emergency_parks" 2>/dev/null || echo 0) + 1 )) > "$STATE/emergency_parks"; done
        exit 0
      fi
    fi
    [ "$ht" -eq "$HIGH_TICKS" ] && log "load1 $load >= $LOAD_MAX: holding (LOAD_PARK=0: no park, no unpark)"
    exit 0
  fi
  for i in "${idle[@]}"; do park "$i" "load1 $load >= $LOAD_MAX, $ht ticks"; done
  exit 0
fi
rm -f "$STATE/high_ticks" "$STATE/emergency_ticks"
# Over the limit (CAP minus other jobs): park idle runners, highest index first.
over=$(( ${#run[@]} - limit ))
if [ "$over" -gt 0 ]; then
  for ((k=${#idle[@]}-1; k>=0 && over>0; k--)); do park "${idle[$k]}" "cap $CAP, other jobs $nother"; over=$((over-1)); done
  exit 0
fi
# Under the cap: unpark, lowest index first, after the hold time; not while the ladder holds (L1+).
[ "$LADDER" = enforce ] && [ "$L" -ge 1 ] && exit 0
last=$(cat "$STATE/last_park" 2>/dev/null || echo 0)
[ $(( $(date +%s) - last )) -lt "$HOLD_S" ] && exit 0
need=$(( limit - ${#run[@]} ))
for i in $(seq 1 "$POOL_SIZE"); do
  [ "$need" -gt 0 ] || break
  running "$i" && continue
  unpark "$i" "load1 $load < $LOAD_MAX, ${#run[@]}/$limit running (cap $CAP, other jobs $nother)"; need=$((need-1))
done
