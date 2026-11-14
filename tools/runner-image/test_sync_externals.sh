#!/bin/bash
# shellcheck disable=SC2034  # out and rc are read inside the eval strings of ok()
# Self-test for sync-externals.sh with a stub docker (no daemon, temp dirs only).
set -u
S="$(cd "$(dirname "$0")" && pwd)/sync-externals.sh"
T=$(mktemp -d); trap 'rm -rf "$T"' EXIT
fail=0
ok() { if eval "$2"; then echo "ok   $1"; else echo "FAIL $1"; fail=1; fi; }
# Stub: the "image" is the dir $T/img/<name>; create prints the name, cp copies, rm -f logs.
mkdir -p "$T/bin"
cat > "$T/bin/docker" <<'D'
#!/bin/bash
case "$1" in
  create) [ -d "$STUB_IMG/$2" ] || { echo "no such image" >&2; exit 1; }; echo "$2" ;;
  cp) src="${2#*:}"; name="${2%%:*}"; cp -a "$STUB_IMG/$name${src%/.}/." "$3" ;;
  rm) echo "$3" >> "$STUB_IMG/../removed" ;;
esac
D
chmod 755 "$T/bin/docker"
export PATH="$T/bin:$PATH" STUB_IMG="$T/img"
mk() { mkdir -p "$T/img/$1/actions-runner/externals/node24/bin"
       printf '#!/bin/sh\necho %s\n' "$2" > "$T/img/$1/actions-runner/externals/node24/bin/node"
       chmod 755 "$T/img/$1/actions-runner/externals/node24/bin/node"
       ln -sfn node24 "$T/img/$1/actions-runner/externals/node20"; }
mk v1 one; mk v2 two
D="$T/host/actions-runner/externals"
out=$(bash "$S" v1 "$D" 2>&1); rc=$?
ok "fresh host: created, exit 0" "[ $rc -eq 0 ] && echo \"\$out\" | grep -q 'created from v1'"
ok "node24 present and runnable" "[ \"\$($D/node24/bin/node)\" = one ]"
ok "node20 -> node24 link kept" "[ \"\$(readlink $D/node20)\" = node24 ]"
ok "dir readable by others (755)" "[ \"\$(stat -c %a $D)\" = 755 ]"
ok "temp container removed" "grep -qx v1 $T/removed"
out=$(bash "$S" v1 "$D" 2>&1); rc=$?
ok "same image again: nothing changed" "[ $rc -eq 0 ] && echo \"\$out\" | grep -q 'already matches'"
out=$(bash "$S" v2 "$D" 2>&1); rc=$?
ok "new image: updated" "[ $rc -eq 0 ] && echo \"\$out\" | grep -q 'updated from v2' && [ \"\$($D/node24/bin/node)\" = two ]"
mkdir -p "$T/img/bad/actions-runner/externals/node16"
out=$(bash "$S" bad "$D" 2>&1); rc=$?
ok "image without node24: fails loud, host copy kept" "[ $rc -ne 0 ] && echo \"\$out\" | grep -q FAILED && [ \"\$($D/node24/bin/node)\" = two ]"
out=$(bash "$S" missing "$D" 2>&1); rc=$?
ok "docker create fails: non-zero, host copy kept" "[ $rc -ne 0 ] && echo \"\$out\" | grep -q 'FAILED: docker create' && [ -x $D/node24/bin/node ]"
ok "no temp or .old dirs left" "[ -z \"\$(ls -d $D.new.* $D.old 2>/dev/null)\" ]"
out=$(bash "$S" 2>&1); rc=$?
ok "no image argument: usage, non-zero" "[ $rc -ne 0 ] && echo \"\$out\" | grep -q usage"
[ $fail -eq 0 ] && echo "all ok"; exit $fail
