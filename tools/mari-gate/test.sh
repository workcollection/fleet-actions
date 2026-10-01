#!/bin/bash
# Scenario tests for mari-gate.sh with a stub `docker` (no docker or runners needed).
# Usage: bash test.sh   -> prints PASS/FAIL per scenario, exits non-zero on any FAIL.
set -u
here=$(cd "$(dirname "$0")" && pwd); tmp=$(mktemp -d); trap 'rm -rf "$tmp"' EXIT
mkdir -p "$tmp/bin"
cat > "$tmp/bin/docker" <<'STUB'
#!/bin/bash
case "$1" in
  ps)  for i in $RUNNING; do echo "runner-$i"; done ;;
  top) i=${2#runner-}; echo "PID COMMAND"; for b in $BUSY; do [ "$b" = "$i" ] && echo "1 Runner.Worker"; done ;;
  stop|rm) echo "docker $*" >> "$MG/calls" ;;
esac
STUB
printf '#!/bin/sh\necho "launch $1" >> "$MG/calls"\n' > "$tmp/launch.sh"; chmod +x "$tmp/bin/docker" "$tmp/launch.sh"
fail=0
# t <name> <load1> <running> <busy> <last_park_age_s|""> <expected stops> <expected launches>
t(){ export MG=$(mktemp -d -p "$tmp"); : > "$MG/calls"; echo "$2 1 1 1/1 1" > "$MG/loadavg"; mkdir -p "$MG/st"
  printf 'RUNNER_PREFIX=runner-\nPOOL_SIZE=10\nCAP=6\nLOAD_MAX=8\nHOLD_S=120\nLAUNCH_CMD=%s\nSTATE=%s/st\nLOADAVG=%s/loadavg\nMODE=enforce\n' "$tmp/launch.sh" "$MG" "$MG" > "$MG/conf"
  [ -n "$5" ] && echo $(( $(date +%s) - $5 )) > "$MG/st/last_park"
  RUNNING="$3" BUSY="$4" MARI_GATE_CONF="$MG/conf" PATH="$tmp/bin:$PATH" bash "$here/mari-gate.sh" >/dev/null 2>&1
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
exit $fail
