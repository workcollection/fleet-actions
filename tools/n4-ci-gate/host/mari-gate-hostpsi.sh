#!/bin/sh
# Feed CT113 (mari-gate, pn-agent via n4-ci-gate) with the HOST CPU pressure (emergency valve, m-4179; the shared
# start rule, n4-ci-gate), the max CPU pressure of the game wings (throttle ladder input, m-4957: 110 ptero,
# 114 palworld, 115 steam, 116 mc) and the host package temperature (n4-ci-gate thermal stop).
# A file is only replaced when its source was read; on failure it goes stale and the gates hold.
put(){ pct exec 113 -- sh -c "mkdir -p /run/mari-gate && cat > /run/mari-gate/$1.tmp && mv /run/mari-gate/$1.tmp /run/mari-gate/$1"; }
rc=0
put host-cpu-pressure < /proc/pressure/cpu || rc=1
if w=$(/usr/local/sbin/wing-psi 110 114 115 116); then printf "%s\n" "$w" | put wing-cpu-pressure || rc=1
else rc=1; fi
# coretemp 'Package id 0' (millidegrees); the hwmon index can change across boots, so find it by name
pkg=""
for h in /sys/class/hwmon/hwmon*; do
    [ "$(cat "$h/name" 2>/dev/null)" = coretemp ] || continue
    for l in "$h"/temp*_label; do
        [ "$(cat "$l" 2>/dev/null)" = "Package id 0" ] && pkg="${l%_label}_input"
    done
done
if [ -n "$pkg" ] && [ -r "$pkg" ]; then put host-pkg-temp < "$pkg" || rc=1; else rc=1; fi
exit $rc
