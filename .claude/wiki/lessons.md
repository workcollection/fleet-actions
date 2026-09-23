---
tags: ci, security, runners, release, gotchas
summary: What the 2026-09 update pass taught about this repo — fork guard rationale, self-hosted runner limits (container actions and bind mounts fail), API facts behind pvr-check, the stacked-PR trap, and how a release is verified before a tag is cut
---
# Lessons (2026-09 update pass)

Read before changing a workflow here: a bad commit reaches every consumer.

## Fork PRs and self-hosted runners
- A reusable workflow sees the CALLER's event: `github.event_name`, `github.repository`
  and `github.event.pull_request.head.repo` all resolve to the caller. That is why the
  fork guard can live here and protect every consumer at once.
- The guard expression: `runs-on: ${{ (PR event) && head.repo != github.repository && 'ubuntu-latest' || inputs.runs-on }}`.
  A deleted fork (`head.repo` null) fails safe to `ubuntu-latest`.
- `pull_request_target` is never acceptable with a checkout of the PR head; the
  fleet-check detector flags it in any trigger form (bare, list, block) — parse YAML,
  do not grep. Per-job evaluation matters: a file that mixes a guarded fleet call with
  its own self-hosted PR job must flag only that job, and the standard shape (PR jobs
  on `ubuntu-latest` + a main-gated self-hosted deploy) must not be flagged.

## Self-hosted runner limits (measured)
- Container actions (`uses:` with a Docker action, e.g. hadolint-action) cannot see the
  checkout on the fleet's containerised runners; `docker run -v $PWD/...` bind mounts
  arrive as empty directories. Run tools through stdin/flags: `docker run -i <image> … - < file`.
- Runner version was 2.337.0 at the time, so Node 24 actions (docker/* v4/v7) are fine.
- Composite steps have no `continue-on-error`; a step that may fail must swallow its
  own error and report through an output (see `setup-php`).

## API facts behind `pvr-check`
- `GET /repos/{r}/private-vulnerability-reporting` → `{"enabled":bool}` for public
  repos (even unauthenticated), 404 for private repos → "not applicable".
- The community-profile endpoint does not reflect an organisation's default
  `SECURITY.md`; read `<owner>/.github/SECURITY.md` directly and name the source.
- User accounts have no organisation default, so their public repos carry the file.

## Pinning and downloads
- Third-party actions are SHA-pinned with a version comment; Dependabot updates both.
- Downloaded binaries are verified against the release checksum file before running.
  osv-scanner v2 exit codes: 0 clean, 1 findings, 128 nothing to scan, else error.

## Release mechanics
- Never stack a PR on a branch that is deleted at merge: GitHub closes the stacked PR
  and a closed PR cannot be retargeted. Target `main`, rebase.
- Tags `v*` are immutable by ruleset; `main` requires a PR. Self-references inside the
  reusable workflows (`…/.github/actions/x@vN`) are bumped in the release commit.
- Before a tag: this repo's CI (lint + smoke execution of every composite + the
  pvr-check dogfood) AND a consumer run on the fleet's self-hosted runners via a draft
  PR that points the consumer's stubs at the release-candidate SHA.
- Migration order for consumers: unprotected repos first — reusable workflows report
  checks as `<caller> / <called>`, and a protected repo's required-checks list must
  change in the same PR. See `docs/TAG-PLAN.md`.

## Rolling out a NEW workflow file to consumers
- A workflow that does not yet exist on the repository's default branch cannot be
  `workflow_dispatch`ed on a PR branch — GitHub answers 404 for the dispatch. Only
  workflows already on the default branch can be dispatched on other refs.
- So a consumer stub for a new reusable workflow needs a `pull_request` trigger if the
  rollout PR is to be gated on a real run of it; otherwise the PR shows no check and the
  first run only happens after merge. The fork guard inside the reusable workflow keeps
  the `pull_request` trigger safe for public repos.
- Measured 2026-09-23 on the pvr-check rollout: six PRs held by the gate with "no jobs
  ran" until the stub gained `pull_request`; after regenerating the branches all six ran
  and merged within ~20 minutes.
