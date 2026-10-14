#!/bin/bash
# Scenario tests for mari-gate.sh with a stub `docker` (no docker or runners needed).
# Usage: bash test.sh   -> prints PASS/FAIL per scenario, exits non-zero on any FAIL.
set -u
here=$(cd "$(dirname "$0")" && pwd); tmp=$(mktemp -d); trap 'rm -rf "$tmp"' EXIT
mkdir -p "$tmp/bin"
cat > "$tmp/bin/docker" <<'STUB'
#!/bin/bash
case "$1" in
  ps)  for i in $RUNNING; do echo "runner-$i"; done; for o in ${OTHER:-}; do echo "$o"; done ;;
  top) i=${2#runner-}; echo "PID COMMAND"; for b in $BUSY; do [ "$b" = "$i" ] && echo "1 Runner.Worker"; done
       # LATE: idle at the first look, busy (a worker) from the second look on
       for b in ${LATE:-}; do [ "$b" = "$i" ] || continue; n=$(cat "$MG/top.$i" 2>/dev/null || echo 0); echo $((n+1)) > "$MG/top.$i"; [ "$n" -ge 1 ] && echo "1 Runner.Worker"; done ;;
  logs) i=${4#runner-}; for b in ${ASSIGNED:-}; do [ "$b" = "$i" ] && echo "2026-10-07 06:35:47Z: Running job: security / Go security"; done
        for b in ${DONE:-}; do [ "$b" = "$i" ] && { echo "2026-10-07 06:35:47Z: Running job: x"; echo "2026-10-07 06:36:10Z: Job x completed with result: Succeeded"; }; done
        # JUSTDONE: finished a job 5 s ago (stamped now-5s)
        for b in ${JUSTDONE:-}; do [ "$b" = "$i" ] && { echo "$(date -u -d @$(( $(date +%s) - 5 )) '+%F %TZ'): Job y completed with result: Succeeded"; }; done ;;
  stop|rm) echo "docker $*" >> "$MG/calls" ;;
esac
STUB
printf '#!/bin/sh\necho "launch $1" >> "$MG/calls"\nif [ -n "$LAUNCH_FAIL" ]; then echo "docker: Error response from daemon: Conflict. The container name is already in use" >&2; echo "Run '"'"'docker run --help'"'"' for more information" >&2; exit 125; fi\n' > "$tmp/launch.sh"
printf '#!/bin/sh\necho "$1" >> "$MG/alerts"\n' > "$tmp/alert.sh"; chmod +x "$tmp/alert.sh"; chmod +x "$tmp/bin/docker" "$tmp/launch.sh"
fail=0
# t <name> <load1> <running> <busy> <last_park_age_s|""> <expected stops> <expected launches>
t(){ export MG=$(mktemp -d -p "$tmp"); : > "$MG/calls"; echo "$2 1 1 1/1 1" > "$MG/loadavg"; mkdir -p "$MG/st"
  printf 'RUNNER_PREFIX=runner-\nPOOL_SIZE=10\nCAP=6\nLOAD_MAX=8\nHOLD_S=120\nLAUNCH_CMD=%s\nSTATE=%s/st\nLOADAVG=%s/loadavg\nMODE=enforce\nOTHER_JOB_PREFIX=%s\nFAIL_ALERT=3\nALERT_CMD=%s\nHIGH_TICKS=%s\nMIN_IDLE_S=%s\nLOAD_PARK=%s\nEMERGENCY_LOAD=%s\nEMERGENCY_TICKS=3\nEMERGENCY_PSI=30\nPSI_FILE=%s/psi\n' "$tmp/launch.sh" "$MG" "$MG" "${OPFX:-}" "$tmp/alert.sh" "${HT:-1}" "${MINIDLE:-0}" "${LP:-1}" "${EL:-0}" "$MG" > "$MG/conf"
  [ -n "${PSI:-}" ] && echo "some avg10=$PSI avg60=1.00 avg300=1.00 total=1" > "$MG/psi"
  [ -n "${PSI_OLD:-}" ] && touch -d '10 minutes ago' "$MG/psi"
  [ -n "${ET:-}" ] && echo "$ET" > "$MG/st/emergency_ticks"
  [ -n "${HIGH:-}" ] && echo "$HIGH" > "$MG/st/high_ticks"
  [ -n "$5" ] && echo $(( $(date +%s) - $5 )) > "$MG/st/last_park"
  [ -n "${STREAK:-}" ] && echo "$STREAK" > "$MG/st/unpark_fail"
  RUNNING="$3" BUSY="$4" OTHER="${OTHER:-}" LAUNCH_FAIL="${LAUNCH_FAIL:-}" LATE="${LATE:-}" ASSIGNED="${ASSIGNED:-}" DONE="${DONE:-}" JUSTDONE="${JUSTDONE:-}" MARI_GATE_CONF="$MG/conf" PATH="$tmp/bin:$PATH" bash "$here/mari-gate.sh" >/dev/null 2>&1
  stops=$(grep 'docker stop' "$MG/calls" | awk '{print $NF}' | sed 's/runner-//' | sort -n | tr '\n' ' ' | sed 's/ $//')
  launches=$(grep launch "$MG/calls" | awk '{print $2}' | sort -n | tr '\n' ' ' | sed 's/ $//')
  if [ "$stops" = "$6" ] && [ "$launches" = "$7" ]; then echo "PASS $1"; else echo "FAIL $1: stops=[$stops] want [$6], launches=[$launches] want [$7]"; fail=1; fi; }
t "high load parks idle runners only"   9.5 "1 2 3 4 5 6 7 8 9 10" "1 2 3"             ""  "4 5 6 7 8 9 10" ""
t "load exactly LOAD_MAX parks"         8.0 "1 2"                  ""                  ""  "1 2"            ""
t "over the cap parks highest idle"     2.0 "1 2 3 4 5 6 7 8 9 10" "1 2"               ""  "7 8 9 10"       ""
t "over the cap never parks busy ones"  2.0 "1 2 3 4 5 6 7 8 9 10" "1 2 3 4 5 6 7 8 9" ""  "10"             ""
t "under the cap after the hold"        2.0 "1 2 3"                "1"                 300 ""               "4 5 6"
t "under the cap within the hold"       2.0 "1 2 3"                "1"                 30  ""               ""
t "at the cap does nothing"             2.0 "1 2 3 4 5 6"          ""                  ""  ""               ""
# Another pool's jobs (OTHER_JOB_PREFIX) count against CAP; the gate never touches them.
OPFX=pp- OTHER="pp-a pp-b"
t "other jobs shrink the cap"           2.0 "1 2 3 4 5 6"          ""                  ""  "5 6"            ""
t "other jobs: busy runners stay"       2.0 "1 2 3 4 5 6"          "1 2 3 4 5"         ""  "6"              ""
t "other jobs limit the unpark"         2.0 "1 2"                  ""                  300 ""               "3 4"
OTHER="pp-a pp-b pp-c pp-d pp-e pp-f pp-g"
t "more other jobs than CAP"            2.0 "1 2"                  "1"                 ""  "2"              ""
OPFX="" OTHER="pp-a pp-b"
t "prefix unset ignores others"         2.0 "1 2 3 4 5 6"          ""                  ""  ""               ""
OPFX=pp- OTHER="xpp-a runner-pp"
t "prefix must match the name start"    2.0 "1 2 3 4 5 6"          ""                  ""  ""               ""
# HIGH_TICKS=2: one high tick waits (no park, no unpark); the second parks; a low tick resets.
HT=2
t "1st high tick waits"                   9.5 "1 2 3"                ""                  300 ""               ""
HIGH=1 t "2nd high tick parks idle"       9.5 "1 2 3"                "1"                 ""  "2 3"            ""
HIGH=""
HIGH=1 t "a low tick resets, unparks"     2.0 "1 2 3"                ""                  300 ""               "4 5 6"
[ ! -e "$MG/st/high_ticks" ] && echo "PASS low tick removed high_ticks" || { echo "FAIL high_ticks not reset"; fail=1; }
HT="" HIGH=""
# LOAD_PARK=0: high load holds; nothing is parked or unparked. Low load still unparks.
LP=0
t "LOAD_PARK=0: high load parks nothing"  9.5 "1 2 3"                ""                  ""  ""               ""
t "LOAD_PARK=0: high load, no unpark"      9.5 "1 2"                  ""                  300 ""               ""
t "LOAD_PARK=0: low load still unparks"    2.0 "1 2"                  ""                  300 ""               "3 4 5 6"
# Emergency valve (EMERGENCY_TICKS=3 here): parks only with load1 >= EMERGENCY_LOAD for 3 ticks AND fresh PSI >= 30.
EL=16
ET=2 PSI=45 t "valve: 3rd tick at 17 + PSI 45 parks idle" 17.0 "1 2 3"  "1"            ""  "2 3"            ""
ET=1 PSI=45 t "valve: only 2 ticks: hold"          17.0 "1 2 3"         "1"            ""  ""               ""
ET=2 PSI=12 t "valve: PSI 12 < 30: hold"           17.0 "1 2 3"         "1"            ""  ""               ""
ET=2 PSI=45 PSI_OLD=1 t "valve: stale PSI file: hold" 17.0 "1 2 3"      "1"            ""  ""               ""
ET=2 PSI=45 t "valve: load 12 < 16: hold"          12.0 "1 2 3"         "1"            ""  ""               ""
EL=0 ET=9 PSI=99 t "valve off (EMERGENCY_LOAD=0): hold" 17.0 "1 2 3"    "1"            ""  ""               ""
EL="" ET="" PSI="" PSI_OLD=""
LP=""
# The park race: a runner that takes a job after the idle check is not stopped.
LATE="5 6"
t "re-check spares a runner that took a job" 9.5 "1 2 3 4 5 6"        ""                  ""  "1 2 3 4"        ""
LATE="" ASSIGNED="3"
t "'Running job:' without completion = busy" 9.5 "1 2 3 4"             ""                  ""  "1 2 4"          ""
ASSIGNED="" DONE="2"
t "completed job = idle again"            9.5 "1 2 3"                ""                  ""  "1 2 3"          ""
DONE=""
# MIN_IDLE_S=30: a runner idle for only 5 s (a backlog job is on its way) is not parked; one idle
# for minutes (DONE, 06:36Z) or with no completion line at all is.
MINIDLE=30 JUSTDONE="2 3" DONE="4"
t "MIN_IDLE_S spares just-finished runners" 9.5 "1 2 3 4"           ""                  ""  "1 4"            ""
MINIDLE="" JUSTDONE="2 3" DONE=""
t "MIN_IDLE_S=0 parks them (old behaviour)" 9.5 "1 2 3"             ""                  ""  "1 2 3"          ""
JUSTDONE=""
# A launcher that fails: the streak counts, the error is logged, ALERT_CMD fires at FAIL_ALERT (3 here).
OPFX="" OTHER="" LAUNCH_FAIL=1
fs(){ # fs <name> <start streak|""> <want streak> <want alerts>
  STREAK="$2" t "$1 (launches)" 2.0 "1 2 3" "1" 300 "" "4 5 6" >/dev/null
  got=$(cat "$MG/st/unpark_fail" 2>/dev/null || echo 0); al=$(grep -c . "$MG/alerts" 2>/dev/null || echo 0)
  st=$(grep -o 'unpark_fail=[0-9]*' "$MG/st/status")
  if [ "$got" = "$3" ] && [ "$al" = "$4" ]; then echo "PASS $1 (streak $got, alerts $al, $st)"; else echo "FAIL $1: streak $got want $3, alerts $al want $4"; fail=1; fi; }
fs "failed unparks count up"             ""  3 1
fs "streak continues, no repeat alert"   3   6 0
fs "repeat alert at 12*FAIL_ALERT"       34  37 1
LAUNCH_FAIL="" STREAK=5 t "a successful unpark resets the streak" 2.0 "1 2 3" "1" 300 "" "4 5 6"
[ ! -e "$MG/st/unpark_fail" ] && echo "PASS streak file removed on success" || { echo "FAIL streak file still there"; fail=1; }
# Throttle ladder. lt <name> <wing psi|"" (missing)> <level before> <ticks before "u1 u2 u3 calm"> <cpu.max before>
#   <want level> <want cpu.max> <want launches> [busy] [running]. Runners 1-3 run, runner 1 busy, HOLD_S passed,
#   so a tick that does not hold unparks 4 5 6.
lt(){ export MG=$(mktemp -d -p "$tmp"); : > "$MG/calls"; echo "2.0 1 1 1/1 1" > "$MG/loadavg"; mkdir -p "$MG/st" "$MG/cg"
  printf 'RUNNER_PREFIX=runner-\nPOOL_SIZE=10\nCAP=6\nLOAD_MAX=8\nHOLD_S=120\nLAUNCH_CMD=%s\nSTATE=%s/st\nLOADAVG=%s/loadavg\nMODE=enforce\nLOAD_PARK=0\nLADDER=%s\nLADDER_PSI_FILE=%s/wing\nLADDER_CGROUP=%s/cg\nLADDER_CPU3="%s"\n' \
    "$tmp/launch.sh" "$MG" "$MG" "${LADDER:-enforce}" "$MG" "$MG" "${CPU3:-200000 100000}" > "$MG/conf"
  echo "$6" > "$MG/cg/cpu.max"; echo $(( $(date +%s) - 300 )) > "$MG/st/last_park"
  [ -n "$2" ] && echo "some avg10=$2 max_of=116" > "$MG/wing"
  [ -n "${STALE_FILE:-}" ] && touch -d '5 minutes ago' "$MG/wing"
  [ -n "$3" ] && echo "$3" > "$MG/st/ladder_level"
  [ -n "$4" ] && echo "$4" > "$MG/st/ladder_ticks"
  [ -n "${SAVED:-}" ] && echo "$SAVED" > "$MG/st/ladder_saved"
  [ -n "${STALE_SINCE:-}" ] && echo $(( $(date +%s) - STALE_SINCE )) > "$MG/st/ladder_stale_since"
  [ -n "${L3_SINCE:-}" ] && echo $(( $(date +%s) - L3_SINCE )) > "$MG/st/ladder_l3_since"
  RUNNING="${10:-1 2 3}" BUSY="${9:-1}" OTHER="" LAUNCH_FAIL="" LATE="" ASSIGNED="" DONE="${DONE_L:-}" JUSTDONE="" MARI_GATE_CONF="$MG/conf" PATH="$tmp/bin:$PATH" bash "$here/mari-gate.sh" ${ARG:-} >/dev/null 2>"$MG/err"
  # The ladder must never write to stderr: a shell error inside it aborts the ladder silently.
  if grep -q . "$MG/err"; then echo "FAIL ladder: $1: stderr: $(head -2 "$MG/err" | tr '\n' ' ')"; fail=1; return; fi
  lvl=$(cat "$MG/st/ladder_level" 2>/dev/null || echo 0); cpu=$(cat "$MG/cg/cpu.max")
  launches=$(grep launch "$MG/calls" | awk '{print $2}' | sort -n | tr '\n' ' ' | sed 's/ $//')
  stops=$(grep 'docker stop' "$MG/calls" | awk '{print $NF}' | sed 's/runner-//' | tr '\n' ' ' | sed 's/ $//')
  if [ "$lvl" = "$5" ] && [ "$cpu" = "$7" ] && [ "$launches" = "$8" ] && [ "$stops" = "${WANT_STOPS:-}" ]; then echo "PASS ladder: $1"
  else echo "FAIL ladder: $1: level $lvl want $5, cpu.max '$cpu' want '$7', launches [$launches] want [$8], stops [$stops] want [${WANT_STOPS:-}]"; fail=1; fi; }
M="max 100000"
lt "calm input at L0: unparks, no write"              5    ""  ""         0 "$M" "$M" "4 5 6"
lt "2 ticks >= 10: still L0"                          12   0   "1 0 0 0" 0 "$M" "$M" "4 5 6"
lt "3rd tick >= 10: L1 holds (no unpark), no write"   12   0   "2 0 0 0" 1 "$M" "$M" ""
lt "L1, 5th tick >= 15: L2 clamps 4 CPUs"             16   1   "4 4 0 0" 2 "$M" "400000 100000" ""
[ "$(cat "$MG/st/ladder_saved")" = "$M" ] && echo "PASS ladder: L2 saved the previous cpu.max" || { echo "FAIL ladder: saved value"; fail=1; }
SAVED="$M"
lt "L2, 10th tick >= 25: L3 clamps 2 CPUs"            30   2   "9 9 9 0" 3 "400000 100000" "200000 100000" ""
lt "one level per tick: L1 -> L2 even at 99 %"        99   1   "9 9 9 0" 2 "$M" "400000 100000" ""
lt "L2, 4 calm ticks: stays"                          5    2   "0 0 0 3" 2 "400000 100000" "400000 100000" ""
lt "L2, 5th calm tick: L1, restores exactly"          5    2   "0 0 0 4" 1 "400000 100000" "$M" ""
[ ! -e "$MG/st/ladder_saved" ] && echo "PASS ladder: saved value cleared after restore" || { echo "FAIL ladder: saved value kept"; fail=1; }
SAVED="123000 100000"
lt "restore writes back the SAVED value, not max"     5    2   "0 0 0 4" 1 "400000 100000" "123000 100000" ""
SAVED="$M"
lt "a calm tick resets on a high one"                 20   2   "0 5 0 3" 2 "400000 100000" "400000 100000" ""
lt "L1 calm x5: L0 unparks again"                     3    1   "0 0 0 4" 0 "$M" "$M" "4 5 6"
lt "reboot reset the cgroup at L2: re-written"        16   2   "0 5 0 0" 2 "$M" "400000 100000" ""
CPU3="100000 100000"
lt "floor: an L3 value below 2 CPUs is raised to 2"   30   2   "9 9 9 0" 3 "400000 100000" "200000 100000" ""
CPU3=""
# Stale or missing input: never escalate; hold the level; back to L0 after LADDER_STALE_RESTORE_S.
STALE_FILE=1 lt "stale input at L2 + 99 %: holds L2"  99   2   "9 9 9 0" 2 "400000 100000" "400000 100000" ""
STALE_FILE=""
lt "missing input at L0: stays L0, unparks"           ""   0   "2 0 0 0" 0 "$M" "$M" "4 5 6"
STALE_SINCE=300 lt "missing input 5 min at L2: holds"  ""  2   ""          2 "400000 100000" "400000 100000" ""
STALE_SINCE=700 lt "missing input >10 min: L0, restores" "" 2  ""          0 "400000 100000" "$M" "4 5 6"
STALE_SINCE=""
# L3 park: only after LADDER_PARK_AFTER_S, one IDLE runner per tick, never a busy one.
L3_SINCE=300 WANT_STOPS="" lt "L3 for 5 min: no park"   30  3  "9 9 9 0" 3 "200000 100000" "200000 100000" ""
L3_SINCE=1000 WANT_STOPS="3" lt "L3 for 16 min: parks one idle (highest)" 30 3 "9 9 9 0" 3 "200000 100000" "200000 100000" ""
L3_SINCE=1000 WANT_STOPS="" lt "L3: all busy: parks none" 30 3 "9 9 9 0" 3 "200000 100000" "200000 100000" "" "1 2 3"
L3_SINCE="" WANT_STOPS=""
# observe: levels are tracked and logged; nothing is written, held or parked.
LADDER=observe lt "observe: L2 reached, no write, no hold" 16 1 "4 4 0 0" 2 "$M" "$M" "4 5 6"
# enforce -> observe while clamped: observe restores the saved value (it used to keep the clamp until LADDER=0).
SAVED="$M" LADDER=observe lt "observe after enforce: restores the clamp" 16 2 "4 4 0 0" 2 "400000 100000" "$M" "4 5 6"
[ ! -e "$MG/st/ladder_saved" ] && echo "PASS ladder: observe forgets the saved value" || { echo "FAIL ladder: observe kept the saved value"; fail=1; }
SAVED=""
SAVED=""
# Off with leftovers: LADDER=0 restores the saved value and forgets the ladder (the rollback path).
SAVED="$M" LADDER=0 lt "LADDER=0 with a saved value: restores" 30 3 "" 0 "200000 100000" "$M" "4 5 6"
[ ! -e "$MG/st/ladder_level" ] && [ ! -e "$MG/st/ladder_saved" ] && echo "PASS ladder: off cleans its state" || { echo "FAIL ladder: state left"; fail=1; }
SAVED="$M" ARG=--restore lt "--restore: cpu.max back, ladder reset" 30 3 "9 9 9 0" 0 "200000 100000" "$M" ""
SAVED="" ARG="" LADDER=""
# From an EMPTY state (first deploy): ticks must accumulate across runs. 3 ticks at 12 % -> L1, then 5 at 16 % -> L2.
MG=$(mktemp -d -p "$tmp"); export MG; : > "$MG/calls"; echo "2.0 1 1 1/1 1" > "$MG/loadavg"; mkdir -p "$MG/st" "$MG/cg"; echo "max 100000" > "$MG/cg/cpu.max"
printf 'RUNNER_PREFIX=runner-\nPOOL_SIZE=10\nCAP=6\nLAUNCH_CMD=%s\nSTATE=%s/st\nLOADAVG=%s/loadavg\nMODE=enforce\nLADDER=enforce\nLADDER_PSI_FILE=%s/wing\nLADDER_CGROUP=%s/cg\n' "$tmp/launch.sh" "$MG" "$MG" "$MG" "$MG" > "$MG/conf"
seq_out=""; errs=0
for p in 12 12 12 16 16 16 16 16; do echo "some avg10=$p" > "$MG/wing"
  RUNNING="1 2 3" BUSY="1" OTHER="" MARI_GATE_CONF="$MG/conf" PATH="$tmp/bin:$PATH" bash "$here/mari-gate.sh" >/dev/null 2>>"$MG/err"
  seq_out="$seq_out$(cat "$MG/st/ladder_level" 2>/dev/null || echo 0)"; done
[ ! -s "$MG/err" ] || errs=1
[ "$seq_out" = "00111112" ] && [ "$errs" = 0 ] && [ "$(cat "$MG/cg/cpu.max")" = "400000 100000" ] \
  && echo "PASS ladder: climbs from an empty state over 8 ticks (levels $seq_out)" \
  || { echo "FAIL ladder: from empty state: levels $seq_out want 00111112, cpu.max '$(cat "$MG/cg/cpu.max")', stderr: $(head -2 "$MG/err" | tr '\n' ' ')"; fail=1; }
# wing-psi.sh (host side): max over the given CTs; a missing CT is skipped; none readable = exit 1, no output.
W=$(mktemp -d -p "$tmp"); for c in 110:0.50 114:12.40 116:3.00; do mkdir -p "$W/${c%%:*}"
  echo "some avg10=${c#*:} avg60=0.00 avg300=0.00 total=1" > "$W/${c%%:*}/cpu.pressure"; done
out=$(CGROOT=$W sh "$here/wing-psi.sh" 110 114 115 116)
[ "$out" = "some avg10=12.40 max_of=114 cts=110:0.50,114:12.40,116:3.00" ] && echo "PASS wing-psi: max, missing CT skipped" || { echo "FAIL wing-psi: got '$out'"; fail=1; }
out=$(CGROOT=$W sh "$here/wing-psi.sh" 998 999 2>/dev/null); rc=$?
[ -z "$out" ] && [ "$rc" = 1 ] && echo "PASS wing-psi: nothing readable = no output, exit 1" || { echo "FAIL wing-psi: rc $rc out '$out'"; fail=1; }
exit $fail
