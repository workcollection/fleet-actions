# Rollout tools

Scripts used for the v2.0.0 consumer rollout (see `.claude/wiki/rollout-v2-wave1.md` and
`docs/TAG-PLAN.md`). They drive `fleetpr.py` from the fleet toolkit (`FLEETPR_DIR` points
at its directory): fresh worktree from the default branch, whitelisted files, one PR per
repo, squash-merge, never `--admin`.

| Script | Purpose |
|---|---|
| `rollout-stubs.py` | Replace copy-pasted `security-scan.yml` / `dependabot-auto-merge.yml` with the reusable-workflow stubs, keeping filename, cron minute, default branch and runner. `--dry`, `--no-merge`. |
| `rollout-checks.py` | Add `fleet-check.yml` (and, for public repos, `pvr-check.yml` + `SECURITY.md` for user-account owners) — new files only. `--only fleet-check|pvr-check`. Needs an inventory TSV (`FLEET_INVENTORY`, columns: repo, visibility, …) and `USER_ACCOUNT_OWNERS`. |
| `gate-merge.py` | Poll `gh pr checks` and merge each PR only when its check family (`GATE_PREFIX`, e.g. `security /`, `hygiene /`, `pvr /`) is green; report holds and unrelated reds by cause. |
| `pool-drain.sh` | Wait until no run is queued/in progress across a list of repos; prints minutes — the post-merge pool-occupancy figure used for runner sizing. |

Rules learned the hard way (all in the wiki): merge only on a REAL green run of the check
under test; a workflow file not yet on the default branch cannot be dispatched, so new
stubs need a `pull_request` trigger; count runners by `status=online`; exclude repos whose
jobs ask for a runner label nobody serves from drain figures.
