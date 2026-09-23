# Tag plan and migration notes

Consumers reference this repo **by tag only** (`@v1`, `@v2`), never `@main`: one bad
commit on `main` would otherwise change CI in every fleet repo at once. Release tags
`v*` are immutable (repository ruleset: no delete, no update, no force-move), so a tag
is a promise, not a pointer.

## v1 (2026-08) — current

Composite actions `setup-php`, `setup-python`, `setup-go`, `setup-dotnet`,
`setup-buildx`, `docker-build-push`; reusable workflows `security-scan.yml`,
`dependabot-auto-merge.yml`, `fleet-check.yml`. Consumed by tag `v1`.

## v2 (2026-09) — what changes

| Area | v1 | v2 |
|---|---|---|
| Fork PRs on self-hosted runners | caller's `runs-on` used unconditionally | **fork guard**: every job of every reusable workflow runs on `ubuntu-latest` when the caller's PR head repo is not the base repo |
| Dependabot reaper on a `pull_request` event | job ran (pointless, write token) | **fails** with an `::error` — wire it to `schedule` / `workflow_dispatch` only |
| Composite inputs | interpolated into shell source | passed as environment variables |
| `setup-php` with a non `major.minor` version (`nightly`, `8.4-dev`) | apt path failed | defers to `shivammathur/setup-php` |
| Third-party actions | major tags (`@v7`) | **commit SHAs** + version comment; Dependabot keeps them current |
| `docker-build-push` internals | `docker/login-action` v3, `build-push-action` v6 | v4.6.0 / v7.4.0 (Node 24: runner ≥ 2.327.1; the fleet's ARC pods run 2.337.0) |
| Scanner binaries | unverified downloads, `@latest` tools | checksum-verified actionlint 1.7.12 / gitleaks 8.30.1 / osv-scanner 2.6.0 (`scan source`, explicit exit codes); `semgrep`, `govulncheck`, `gosec` pinned |
| `fleet-check` | conventions only | plus a per-job **fork-exposure detector** (`pull_request_target`, public + `pull_request` + self-hosted) |
| New | — | **`pvr-check.yml`** (private vulnerability reporting + security policy) and `templates/SECURITY.md` |
| Reserved | — | **`catboy-sign.yml`** (see below) |

Nothing in v2 changes an existing input name or default. A v1 caller stub works
unchanged after `@v1` → `@v2`; the two behaviour changes that can surface are the
fork guard (fork PRs now run on `ubuntu-latest`) and the reaper refusing PR events
(a caller wired to `pull_request` turns red on purpose).

### Reserved seam: `catboy-sign.yml` (owned by the catboy-pki project — not implemented here)

The code-signing step of the catboy.systems PKI plans to ship as a reusable workflow
in **this** repository, so every release workflow in the fleet can call one line:

```yaml
sign:
  uses: workcollection/fleet-actions/.github/workflows/catboy-sign.yml@v2   # reserved, lands in a v2.x minor
```

Contract reserved for it, from the PKI plan (§6.7): `workflow_call`; `permissions:
id-token: write, attestations: write`; **fail-open** — a runner without a configured
CA (`ubuntu-latest`, a runner with no signing identity) logs "no CA configured" and
the job succeeds unsigned; signing identity comes from the runner (AppRole for
persistent runners, Kubernetes auth for the ARC scale set) and the repo identity from
the job's GitHub attestation. It is added in a **minor** release (`v2.1`) once the PKI
project delivers it; no consumer changes, callers opt in by adding the job. The name
`catboy-sign.yml` and the path are reserved now so the PKI plan's references to
`fleet-actions/.github/workflows/catboy-sign.yml@…` resolve to one place.

## Migration v1 → v2

1. **Unprotected repos first.** Reusable workflows report status checks as
   `<caller-job> / <called-job>`; a repo whose branch protection lists a required check
   by name silently stops matching when the name changes. v2 does not rename any
   job, so a v1 consumer already on the `caller / called` names keeps them — but a
   repo migrating from a copy-pasted workflow to the reusable one sees new names.
   Migrate the repos without required checks, then handle protected repos one at a
   time: update the required-checks list in the same PR as the stub change.
2. Change `@v1` → `@v2` in the stubs (four files at most: `ci.yml`,
   `security-scan.yml`, `dependabot-auto-merge.yml`, `fleet-check.yml`).
3. If the repo is public, add a `pvr-check` stub and drop `templates/SECURITY.md` in
   (user-account repos) or rely on the org default (`smol-kitten/.github`).
4. Remove any `pull_request` trigger from the Dependabot reaper stub.
5. Roll out with the fleet PR tool (branch → PR → squash-merge, never `--admin`).

## Verification before a tag is cut

- This repo's CI: actionlint + YAML parse, smoke execution of every composite on
  `ubuntu-latest`, `pvr-self`.
- A **consumer run on the fleet's self-hosted runners**: a draft PR in a private
  consumer repo pointing its stubs at the release candidate SHA (all four workflows
  green on `self-hosted`, scanner artifacts present). v2 was verified this way on
  `smol-kitten/cat-guard` before tagging.
- Self-references inside the reusable workflows (`workcollection/fleet-actions/.github/actions/…@vN`)
  are bumped to the new tag in the release commit — a `@v2` workflow must not pull
  `@v1` composites.
