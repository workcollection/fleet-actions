#!/bin/sh
# wing-psi: the input of the mari-gate throttle ladder. Prints one line in /proc/pressure format,
#   some avg10=<max> max_of=<ctid> cts=<id>:<avg10>,...
# where <max> is the highest CPU pressure 'some avg10' among the given Proxmox containers (the
# tenants CI must not hurt). Runs on the HOST; a container cannot read its neighbours' pressure.
# A stopped or missing CT is skipped. If none is readable: no output, exit 1, so the feeder leaves
# the old file to go stale and the ladder holds instead of acting on a guess.
# Usage: wing-psi.sh <ctid> ...      CGROOT (tests) defaults to /sys/fs/cgroup/lxc
CGROOT=${CGROOT:-/sys/fs/cgroup/lxc}
max=; at=; all=
for ct in "$@"; do
  v=$(sed -n 's/^some avg10=\([0-9.]*\).*/\1/p' "$CGROOT/$ct/cpu.pressure" 2>/dev/null)
  [ -n "$v" ] || continue
  all="${all:+$all,}$ct:$v"
  if [ -z "$max" ] || awk -v a="$v" -v b="$max" 'BEGIN{exit !(a > b)}'; then max=$v; at=$ct; fi
done
[ -n "$max" ] || { echo "wing-psi: no readable cpu.pressure for CT(s) $* under $CGROOT" >&2; exit 1; }
echo "some avg10=$max max_of=$at cts=$all"
