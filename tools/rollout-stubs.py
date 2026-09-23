#!/usr/bin/env python3
"""fleet-actions rollout driver: replace copy-pasted security-scan.yml
and dependabot-auto-merge.yml with the consumer stubs @v2.0.0, keeping filename, cron
minute and runner. Usage: rollout_v2.py --repos owner/a,owner/b [--dry] [--no-merge]"""
import sys, os, re, argparse, json
sys.path.insert(0, os.environ.get("FLEETPR_DIR", "."))  # directory holding fleetpr.py (the fleet PR rollout driver)
from fleetpr import rollout, resolve_org_repos

SEC = """# Caller stub → the fleet's single security-scan (gitleaks / OSV / Semgrep).
# Advisory (non-gating). See workcollection/fleet-actions (docs/TAG-PLAN.md).
name: Security Scan

on:
  push:
    branches: [{branch}]
    paths-ignore: ['**.md', 'docs/**', '.claude/**']
  pull_request:
    branches: [{branch}]
  schedule:
    - cron: '{cron}'
  workflow_dispatch:

permissions:
  contents: read

jobs:
  security:
    uses: workcollection/fleet-actions/.github/workflows/security-scan.yml@v2.0.0
    with:
      runs-on: {runner}
"""
DEP = """# Caller stub → the fleet dependabot reaper (merges green minor/patch PRs).
# MUST run on schedule/dispatch, NOT `pull_request`: dependabot-triggered events
# get a read-only token that cannot merge; a scheduled run gets pull-requests:write
# (and the reusable workflow fails on purpose if called from a PR event).
name: Dependabot auto-merge

on:
  schedule:
    - cron: '{cron}'
  workflow_dispatch:

permissions:
  contents: write
  pull-requests: write
  checks: read
  statuses: read

jobs:
  auto-merge:
    uses: workcollection/fleet-actions/.github/workflows/dependabot-auto-merge.yml@v2.0.0
    with:
      runs-on: {runner}
      # Default policy: minor + patch only. Majors held for manual review.
"""

def cron_of(text, default):
    m = re.search(r"cron:\s*['\"]([^'\"]+)['\"]", text)
    return m.group(1) if m else default

def branch_of(wt, old_text=""):
    # 1. the repo's own copy-pasted file names its default branch; 2. origin/HEAD; 3. main
    m = re.search(r"branches:\s*\[\s*([A-Za-z0-9_.-]+)\s*\]", old_text)
    if m: return m.group(1)
    rc = os.popen(f"git -C '{wt}' symbolic-ref --short refs/remotes/origin/HEAD 2>/dev/null").read().strip()
    return rc.split("/")[-1] if rc else "main"

def make_apply(runner):
    def apply(wt):
        files = []
        wf = os.path.join(wt, ".github/workflows")
        sec = os.path.join(wf, "security-scan.yml")
        if not os.path.isfile(sec):
            return {"ok": False, "error": "no security-scan.yml"}
        old = open(sec).read()
        if "workcollection/fleet-actions" in old:
            return {"ok": False, "error": "already on fleet-actions"}
        br = branch_of(wt, old)
        open(sec, "w").write(SEC.format(branch=br, cron=cron_of(old, "26 6 * * 1"), runner=runner))
        files.append(".github/workflows/security-scan.yml")
        dep = os.path.join(wf, "dependabot-auto-merge.yml")
        if os.path.isfile(dep):
            old2 = open(dep).read()
            if "workcollection/fleet-actions" not in old2:
                open(dep, "w").write(DEP.format(cron=cron_of(old2, "17 5 * * *"), runner=runner))
                files.append(".github/workflows/dependabot-auto-merge.yml")
        return {"ok": True, "files": files}
    return apply

BODY = """Migrates the copy-pasted scanner / reaper workflows to the fleet's reusable ones at the
exact immutable tag `workcollection/fleet-actions@v2.0.0` (see its `docs/TAG-PLAN.md`).

- Filenames, cron minute and runner unchanged; the scanners are the same (gitleaks,
  OSV-Scanner, Semgrep, govulncheck/gosec when a go.mod exists), now checksum-verified
  and SHA-pinned upstream, with a fork guard (fork PRs run on `ubuntu-latest`).
- The reaper keeps its schedule/dispatch triggers; it refuses PR events by design.
- Source of truth: `workcollection/fleet-actions` at tag `v2.0.0` (immutable; the file
  `docs/TAG-PLAN.md` there is the migration note).
- Status checks are reported as `security / <job>` and `auto-merge / reap` because a reusable
  workflow's jobs are nested under the caller's job name; this repo has no required checks
  (measured 2026-09-23), so nothing needs updating — if required checks are ever added, use
  those names.
- Dependabot's `github-actions` ecosystem will raise the PR for future fleet-actions releases.
"""

if __name__ == "__main__":
    ap = argparse.ArgumentParser(); ap.add_argument("--repos", required=True); ap.add_argument("--dry", action="store_true"); ap.add_argument("--no-merge", action="store_true")
    a = ap.parse_args()
    wanted = [r.strip() for r in a.repos.split(",") if r.strip()]
    paths = {}
    for org in sorted({r.split("/")[0] for r in wanted}):
        for p in resolve_org_repos(org, "*"):
            url = os.popen(f"git -C '{p}' remote get-url origin 2>/dev/null").read().strip()
            m = re.search(r"github\.com[:/]+([^/]+)/([^/\s]+?)(?:\.git)?$", url)
            if m: paths[f"{m.group(1)}/{m.group(2)}".lower()] = p
    results = []
    for r in wanted:
        p = paths.get(r.lower())
        if not p:
            results.append({"repo": r, "status": "no-clone"}); continue
        runner = "self-hosted"
        res = rollout([p], make_apply(runner), branch="ci/fleet-actions-v2.0.0",
                      title="ci: fleet-actions security-scan + dependabot reaper stubs @v2.0.0",
                      body=BODY, merge=not a.no_merge, dry=a.dry, tel=False)
        results.extend(res)
    for r in results:
        print(json.dumps({k: r.get(k) for k in ("repo", "status", "files", "detail", "pr")}))
