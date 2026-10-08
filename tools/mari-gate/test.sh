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
printf '#!/bin/sh\necho "launch $1" >> "$MG/calls"\nif [ -n "$LAUNCH_FAIL" ]; then echo "Unable to find image x:1 locally"; echo "pull access denied" >&2; exit 1; fi\n' > "$tmp/launch.sh"
printf '#!/bin/sh\necho "$1" >> "$MG/alerts"\n' > "$tmp/alert.sh"; chmod +x "$tmp/alert.sh"; chmod +x "$tmp/bin/docker" "$tmp/launch.sh"
fail=0
# t <name> <load1> <running> <busy> <last_park_age_s|""> <expected stops> <expected launches>
t(){ export MG=$(mktemp -d -p "$tmp"); : > "$MG/calls"; echo "$2 1 1 1/1 1" > "$MG/loadavg"; mkdir -p "$MG/st"
  printf 'RUNNER_PREFIX=runner-\nPOOL_SIZE=10\nCAP=6\nLOAD_MAX=8\nHOLD_S=120\nLAUNCH_CMD=%s\nSTATE=%s/st\nLOADAVG=%s/loadavg\nMODE=enforce\nOTHER_JOB_PREFIX=%s\nFAIL_ALERT=3\nALERT_CMD=%s\nHIGH_TICKS=%s\nMIN_IDLE_S=%s\nLOAD_PARK=%s\n' "$tmp/launch.sh" "$MG" "$MG" "${OPFX:-}" "$tmp/alert.sh" "${HT:-1}" "${MINIDLE:-0}" "${LP:-1}" > "$MG/conf"
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
exit $fail
