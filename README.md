# fleet-actions

Shared composite actions and reusable workflows for the fleet (`polo-nyan`, `smol-kitten`,
`workcollection`).

## Why this repo exists

Before it, the fleet had **zero** shared CI infrastructure.
Measured across 65 repos and 197 workflow files: 0 reusable workflows, 0 composite
actions, 0 shared `uses:` references. Every workflow file was standalone copy-paste, and the copies rotted:

| File | Repos | Byte-exact versions | Job-name sets |
|---|---|---|---|
| `security-scan.yml` | 54 | **50** | **3** (52 share one) |
| `dependabot-auto-merge.yml` | 43 | 36 | **1** (all 43) |

The divergence was never logic — it was randomized cron minutes and action-pin skew. The
four shell blocks inside `security-scan.yml` are byte-identical across 52 repos; the
27-line reaper script is byte-identical across 40. Keeping 50 copies in sync required a
bespoke Python tool that opened one PR per repo on every template change.

## It must be public

`polo-nyan` is a **User** account and the others are Orgs. A private actions repo can only
be consumed inside its own account, so cross-owner sharing requires a public repo. Nothing
here holds secrets — that is exactly how every third-party action already works.

## Consume by tag, never by branch

```yaml
uses: workcollection/fleet-actions/.github/actions/setup-php@v1   # yes
uses: workcollection/fleet-actions/.github/actions/setup-php@main # no
```

One bad commit on `main` would otherwise break CI in ~65 repos simultaneously. `v1` is a
moving tag that only advances after this repo's own CI passes.

## Composite actions

| Action | Replaces | Notes |
|---|---|---|
| `setup-php` | 20 repos, 17 arg variants | Probe → apt-with-retry → upstream fallback. The retry is the point: ARC pods ship no PHP and ~30% of jobs flaked on a transient apt error. |
| `setup-dotnet` | 21 repos, 3 pins | Folds in the `DOTNET_INSTALL_DIR` export that 12 repos duplicated and that silently does nothing if ordered wrong. |
| `setup-python` | 57 repos (52 identical args) | Pin skew only: v7/v6/v5. |
| `setup-go` | 53 repos (52 identical args) | Pin skew only: v7/v6/v5. |
| `setup-buildx` | 21 repos, all zero-arg | Pin skew only: v4/v3/v3.7.1. |
| `docker-build-push` | 7 repos | Defaults to GHA cache `mode=min` with a ref-scoped key. `mode=max` blew past the 10GB per-repo cache cap (one repo sat at 10.7GB, so the cache evicted itself continuously). |

### setup-php: why the odd shape

Composite-action steps do **not** support `continue-on-error`, so any step that fails
takes the whole action down. That rules out "try the upstream action, retry on failure".
Instead:

1. **Probe** — a matching `php` already on `PATH` wins immediately. When PHP is
   eventually baked into the runner image, every caller lands here and the rest never
   runs; the image work becomes invisible rather than requiring 20 repo edits.
2. **apt with a bounded retry loop** — full control over failure handling in bash, which
   is what actually absorbs the transient flake.
3. **`shivammathur/setup-php@v2`** — upstream fallback if our own install did not produce
   a usable `php`.

A final verify step fails loudly *here* rather than three steps later in the caller's
build, where `php: command not found` reads like the repo's own bug.

## Reusable workflows

| Workflow | Replaces |
|---|---|
| `security-scan.yml` | 54 copies / 50 versions. Runner is now an input. |
| `dependabot-auto-merge.yml` | 43 copies / 36 versions. Branch filter and merge method are now inputs. |
| `fleet-check.yml` | Out-of-band Python hygiene scans — drift now fails a PR instead of surfacing weeks later. |

```yaml
jobs:
  security:
    uses: workcollection/fleet-actions/.github/workflows/security-scan.yml@v1
    with:
      runs-on: self-hosted     # or ubuntu-latest where no self-hosted runner exists
```

## Security model

This repo is public and its workflows run on fleet self-hosted runners, so the
threat is code from a fork reaching those runners.

- **This repo's own CI** uses `pull_request` (never `pull_request_target`) on
  `ubuntu-latest` with a read-only token. A fork PR here runs on GitHub's sandbox only.
- **Fork guard in every reusable workflow.** If the caller's event is a pull request
  whose head is another repository, the job ignores `runs-on` and runs on
  `ubuntu-latest`. Same-repo PRs, pushes, schedules and dispatches keep the requested
  runner. Consumers get this for free by calling the workflow.
- **The Dependabot reaper refuses PR events.** It only makes sense on `schedule` /
  `workflow_dispatch`, and it says so in code.
- **Composite inputs never become shell source.** Inputs reach `run:` steps as
  environment variables; a quote in an input cannot become a command.
- **`fleet-check` flags exposure in consumers**: any `pull_request_target` trigger,
  and `pull_request` + self-hosted in a public repo outside the guarded workflows.
- **What consumers must still do:** never combine `pull_request_target` with a
  checkout of the PR head; never run your *own* jobs for `pull_request` on
  self-hosted runners in a public repo unless gated on
  `github.event.pull_request.head.repo.full_name == github.repository`.
- **Settings the org admin holds** (not in git): default workflow token
  permissions read-only, fork-PR approval for all outside collaborators, protected
  `main` and `v*` tags (a moved tag silently changes CI in every consumer), and
  SHA pinning once the actions here are pinned.

## Migrating a repo

**Reusable workflows rename the reported status check** to `<caller-job> / <called-job>`.
Any branch-protection rule naming a required check silently stops matching. Migrate
unprotected repos first, then fix protected repos' required-check names deliberately, one
at a time. Never bulk-convert.

Keep each caller's workflow **filename** unchanged so path filters and existing references
keep working. Roll out with `fleetpr` (branch → PR → squash-merge, never `--admin`).

Two repos are deliberately **not** candidates for `security-scan.yml` without a decision:
`catboyindustries-arg`/`repo` run a different generation (trufflehog/trivy/CodeQL), and
`DropMeNot` runs a reduced 6-job variant.
