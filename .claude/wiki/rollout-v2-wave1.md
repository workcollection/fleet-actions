---
tags: rollout, migration, ci, runners, gotchas
summary: How the v2.0.0 rollout to 69 consumer repos went (2026-09-23) — the gate that demanded real check runs, the one repo whose scan workflow GitHub had silently disabled, the red causes that were all runner infrastructure, the user-account repos with no live runner, and the load figure to size the next wave
---
# Rollout v2.0.0 — wave 1 (2026-09-23)

Consumers moved from copy-pasted `security-scan.yml` / `dependabot-auto-merge.yml` to the
reusable workflows at the exact tag `v2.0.0`: 63 private + 6 public organisation repos, one
PR per repo, keeping each repo's filename, cron minute and runner. 17 user-account repos
stay open and unmerged (see "no live runner").

## The gate: merge only on a real green check run
Each PR was merged only after its `security / *` jobs had **run and passed** — not after
"no failures". That distinction found the most important thing in the wave:

- **A disabled workflow is invisible in the UI and indistinguishable from a passing one
  until you look for check RUNS.** One repo's Security Scan workflow had been auto-disabled
  by GitHub (repository inactivity). The repo *looked* covered — the file was there, the
  last run was green — and it ran nothing. The migration PR simply had no checks at all.
  Fix: `gh workflow enable <file>`, then `gh workflow run … --ref <branch>` and merge on the
  green run whose head SHA equals the PR head. A fleet-wide sweep afterwards
  (`GET /repos/{r}/actions/workflows`, `state != active`) found no other one.
- A `workflow_dispatch` run on the PR branch does not show up in `gh pr checks`; compare
  the run's `headSha` with the PR's `headRefOid` before treating it as the gate.

## Reds, all runner infrastructure, none from the change
| Symptom in the job | Cause | Action |
|---|---|---|
| `…/externals/node24/bin/node: No such file` in `actions/checkout` | runner's externals missing on a nearly-full disk | `gh run rerun --failed`; landed on another runner, green |
| `Can't find 'action.yml' … under _actions/actions/checkout/<sha>` | runner's action cache corrupted | same |
| `Can't use 'tar -xzf' extract archive file` in *Set up job* | disk full on the runner host | same, after the host had space |
| `The runner has received a shutdown signal` (job cancelled) | runner restarted mid-job during host maintenance | same |
| repo's own build job red (e.g. a removed SkiaSharp API after a major bump) | pre-existing, unrelated to the two stub files | merged on green scans, reported to the repo owner, untouched |

Attribute before you rerun: read the failed step's log line, not the job colour. A red that
names a path under the runner's `_work` or `externals` is the host.

## User-account repos: no live runner
The user account cannot have an organisation runner group; runners are per repo. Every
registered runner on those repos was `status=offline` (phantoms left by a purge) or absent,
so their self-hosted jobs queue forever — and had been doing so before the migration. The
inventory read "1 runner" from the phantom registration: **count runners by
`status=online`, not by existence.** Those PRs stay open, unmerged, and merge the moment a
runner answers; moving them to `ubuntu-latest` is an operator decision (paid minutes on
private repos), not a rollout default.

## Sizing a wave
63 repos × 4–5 jobs went through the organisation's self-hosted pool in about 90 minutes
and pushed an already-full runner host to 100% disk (a builder-container leak on the host,
not the wave, held the space). Before the next wave: check free disk on the runner host,
run PRs unmerged (`--no-merge`), gate on green, and keep a wave to what the pool can drain
in an hour.

## Tools (on the fleet toolkit box, not in this repo)
`fleetpr.py` (fresh worktree from origin/default, whitelisted files, squash-merge, never
`--admin`), a per-repo apply function that derives the default branch and cron from the
repo's old file, and a gate that polls `gh pr checks` (plain output — this `gh` has no
`--json` for it) and merges on green `security / *` jobs.
