#!/bin/bash
# Runner job-started hook (ACTIONS_RUNNER_HOOK_JOB_STARTED): drop any stored ghcr.io login before a job.
# A job that runs a plain `docker login ghcr.io` with GITHUB_TOKEN and no logout leaves that token in the
# long-lived runner's docker config. The token expires with the job, and from then on every ghcr pull on
# that runner, public images included, fails with 'failed to fetch oauth token: denied' (n4-runner-2,
# 2026-11-13, smol-kitten/web-fapi dependency-updates.yml). Only the ghcr.io entries go; values are never
# printed. A hook that exits non-zero fails the job, so a problem here is reported and the job goes on.
for dir in "${DOCKER_CONFIG:-}" "${HOME:-/root}/.docker"; do
  f="$dir/config.json"
  [ -n "$dir" ] && [ -f "$f" ] || continue
  hosts=$(jq -r '(.auths // {}) | keys[] | select(test("^(https?://)?ghcr\\.io(/|$)"))' "$f" 2>/dev/null) || {
    echo "::warning::runner hook: $f is not readable JSON; ghcr.io login not checked"; continue; }
  [ -n "$hosts" ] || continue
  tmp=$(mktemp "$f.XXXXXX") || { echo "::warning::runner hook: cannot write next to $f; stale ghcr.io login kept"; continue; }
  if jq '.auths |= with_entries(select(.key | test("^(https?://)?ghcr\\.io(/|$)") | not))' "$f" > "$tmp" \
     && chmod 600 "$tmp" && mv "$tmp" "$f"; then
    echo "runner hook: removed a stored ghcr.io login left by an earlier job ($(echo $hosts | tr ' ' ','), $f)"
  else
    rm -f "$tmp"; echo "::warning::runner hook: could not remove the stored ghcr.io login from $f"
  fi
done
exit 0
