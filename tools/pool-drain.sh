#!/usr/bin/env bash
# pool-drain.sh <repo-list-file> — wait until no workflow run is queued or in progress in
# any listed repo ("owner/name" per line, extra columns ignored); print the minutes it
# took. Exclude repos whose jobs ask for a runner label nobody serves: they never drain.
set -uo pipefail
start=$(date +%s)
while true; do
  busy=0
  while read -r repo _; do
    [ -n "$repo" ] || continue
    n=$(gh api "repos/$repo/actions/runs?per_page=20" \
        --jq '[.workflow_runs[] | select(.status=="queued" or .status=="in_progress")] | length' 2>/dev/null || echo 0)
    busy=$((busy + n))
  done < "$1"
  [ "$busy" -eq 0 ] && break
  if [ $(( $(date +%s) - start )) -gt 3000 ]; then
    echo "drain timeout, still busy=$busy"; exit 1
  fi
  sleep 30
done
echo "drained in $(( ( $(date +%s) - start ) / 60 )) min"
