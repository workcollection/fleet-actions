#!/bin/bash
# Self-test for job-started.sh: runs it against temp docker configs only (never the real one).
set -u
H="$(cd "$(dirname "$0")" && pwd)/job-started.sh"
T=$(mktemp -d); trap 'rm -rf "$T"' EXIT
fail=0
ok() { if eval "$2"; then echo "ok   $1"; else echo "FAIL $1"; fail=1; fi; }
mkdir -p "$T/a/.docker"
cat > "$T/a/.docker/config.json" <<'J'
{"auths":{"ghcr.io":{"auth":"c2VjcmV0"},"https://ghcr.io":{"auth":"c2VjcmV0"},"docker.io":{"auth":"a2VlcA=="}},"currentContext":"default"}
J
out=$(HOME="$T/a" DOCKER_CONFIG= bash "$H"); rc=$?
ok "exit 0" "[ $rc -eq 0 ]"
ok "both ghcr.io entries removed" "[ \"\$(jq -c '.auths|keys' "$T/a/.docker/config.json")\" = '[\"docker.io\"]' ]"
ok "other keys kept" "[ \"\$(jq -r .currentContext "$T/a/.docker/config.json")\" = default ]"
ok "says what it removed" "echo \"\$out\" | grep -q 'removed a stored ghcr.io login'"
ok "never prints a credential" "! echo \"\$out\" | grep -q 'c2VjcmV0'"
ok "file stays 600" "[ \"\$(stat -c %a "$T/a/.docker/config.json")\" = 600 ]"
out=$(HOME="$T/a" DOCKER_CONFIG= bash "$H")
ok "second run is silent (nothing left)" "[ -z \"\$out\" ]"
mkdir -p "$T/b/.docker"; echo '{"auths":{}}' > "$T/b/.docker/config.json"; cp "$T/b/.docker/config.json" "$T/b.orig"
out=$(HOME="$T/b" DOCKER_CONFIG= bash "$H"); rc=$?
ok "clean config untouched and silent" "[ $rc -eq 0 ] && [ -z \"\$out\" ] && cmp -s "$T/b/.docker/config.json" "$T/b.orig""
mkdir -p "$T/c/.docker"; echo 'not json' > "$T/c/.docker/config.json"
out=$(HOME="$T/c" DOCKER_CONFIG= bash "$H"); rc=$?
ok "unreadable config warns, exit 0, file kept" "[ $rc -eq 0 ] && echo \"\$out\" | grep -q '::warning::' && grep -q 'not json' "$T/c/.docker/config.json""
out=$(HOME="$T/none" DOCKER_CONFIG= bash "$H"); rc=$?
ok "no config: exit 0, silent" "[ $rc -eq 0 ] && [ -z \"\$out\" ]"
mkdir -p "$T/d"; echo '{"auths":{"ghcr.io":{"auth":"eA=="}}}' > "$T/d/config.json"
HOME="$T/none" DOCKER_CONFIG="$T/d" bash "$H" >/dev/null
ok "DOCKER_CONFIG dir is cleaned too" "[ \"\$(jq -c .auths "$T/d/config.json")\" = '{}' ]"
[ $fail -eq 0 ] && echo "all ok"; exit $fail
