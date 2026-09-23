#!/usr/bin/env python3
"""fleet-actions: ADD fleet-check.yml (advisory) to every candidate, and
for PUBLIC repos also pvr-check.yml + (user-account repos only) SECURITY.md from the fleet
template. Never touches existing files. Usage: rollout_wave2.py --repos a,b [--dry] [--no-merge]"""
import sys, os, re, argparse, json, subprocess
sys.path.insert(0, os.environ.get("FLEETPR_DIR", "."))  # directory holding fleetpr.py (the fleet PR rollout driver)
from fleetpr import rollout, resolve_org_repos

FLEET = """# Caller stub → fleet hygiene assertions (license / dependabot / security-scan present,
# fork-exposure detector). Advisory (strict: false) so it reports without reddening the repo.
name: Fleet check

on:
  push:
    branches: [{branch}]
  pull_request:
    branches: [{branch}]
  workflow_dispatch:

permissions:
  contents: read

jobs:
  hygiene:
    uses: workcollection/fleet-actions/.github/workflows/fleet-check.yml@v2.0.0
    with:
      runs-on: {runner}
      strict: false
"""
PVR = """# Caller stub → private vulnerability reporting + security policy check (public repo).
# Advisory (strict: false). See workcollection/fleet-actions docs/TAG-PLAN.md.
name: PVR check

on:
  push:
    branches: [{branch}]
  pull_request:
    branches: [{branch}]
  schedule:
    - cron: '{cron}'
  workflow_dispatch:

permissions:
  contents: read

jobs:
  pvr:
    uses: workcollection/fleet-actions/.github/workflows/pvr-check.yml@v2.0.0
    with:
      runs-on: {runner}
      strict: false
"""
TEMPLATE = open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "templates", "SECURITY.md")).read()

def default_branch(wt):
    for f in ("security-scan.yml", "ci.yml"):
        p = os.path.join(wt, ".github/workflows", f)
        if os.path.isfile(p):
            m = re.search(r"branches:\s*\[\s*([A-Za-z0-9_.-]+)\s*\]", open(p).read())
            if m: return m.group(1)
    rc = os.popen(f"git -C '{wt}' symbolic-ref --short refs/remotes/origin/HEAD 2>/dev/null").read().strip()
    return rc.split("/")[-1] if rc else "main"

def make_apply(repo, public, user_account, runner, only=''):
    def apply(wt):
        files = []; wf = os.path.join(wt, ".github/workflows"); os.makedirs(wf, exist_ok=True)
        br = default_branch(wt)
        fc = os.path.join(wf, "fleet-check.yml")
        if only in ('', 'fleet-check') and not os.path.exists(fc):
            open(fc, "w").write(FLEET.format(branch=br, runner=runner)); files.append(".github/workflows/fleet-check.yml")
        if public and only in ('', 'pvr-check'):
            pv = os.path.join(wf, "pvr-check.yml")
            if not os.path.exists(pv):
                minute = sum(ord(c) for c in repo) % 60
                open(pv, "w").write(PVR.format(branch=br, cron=f"{minute} 7 * * 1", runner=runner)); files.append(".github/workflows/pvr-check.yml")
            if user_account and not any(os.path.exists(os.path.join(wt, p)) for p in ("SECURITY.md", ".github/SECURITY.md", "docs/SECURITY.md")):
                owner, name = repo.split("/")
                body = TEMPLATE.replace("<OWNER>", owner).replace("<REPO>", name)
                body = body.replace("<SECURITY_MAILBOX>", "security@catboy.systems")
                body = re.sub(r" — encrypt with the key published at\n   <PGP_KEY_URL> if the report contains exploit details\.", ". If the report contains exploit details, ask for an\n   encryption key in a private report first.", body)
                body = re.sub(r"<!--.*?-->\n\n", "", body, flags=re.S)
                # do not assert PVR is enabled in the file itself; pvr-check measures that
                body = body.replace("\n   (private vulnerability reporting is enabled for this repository).", "")
                open(os.path.join(wt, "SECURITY.md"), "w").write(body); files.append("SECURITY.md")
        if not files: return {"ok": False, "error": "nothing to add"}
        return {"ok": True, "files": files}
    return apply

BODY = """Wave 2 of the fleet-actions rollout (source of truth: `workcollection/fleet-actions@v2.0.0`,
migration note in its `docs/TAG-PLAN.md`). Adds only NEW files:

- `fleet-check.yml` — fleet hygiene assertions + per-job fork-exposure detector, advisory
  (`strict: false`): it reports in the step summary and never reddens the repo.
- (public repos) `pvr-check.yml` — asserts private vulnerability reporting is enabled and a
  security policy exists; advisory.
- (public repos under a user account) `SECURITY.md` from the fleet template — user accounts
  have no organisation default policy.

New checks appear as `hygiene / Fleet hygiene` and `pvr / PVR + security policy` (reusable
workflow jobs are nested under the caller's job name). This repo has no required checks.
"""

if __name__ == "__main__":
    ap = argparse.ArgumentParser(); ap.add_argument("--repos", required=True); ap.add_argument("--dry", action="store_true"); ap.add_argument("--no-merge", action="store_true"); ap.add_argument("--only", default="", help="fleet-check | pvr-check")
    a = ap.parse_args()
    wanted = [r.strip() for r in a.repos.split(",") if r.strip()]
    inv = {l.split("\t")[0].lower(): l.rstrip("\n").split("\t") for l in open(os.environ.get("FLEET_INVENTORY", "fleet-inventory.tsv")) if l.strip()}
    paths = {}
    for org in sorted({r.split("/")[0] for r in wanted}):
        for p in resolve_org_repos(org, "*"):
            url = os.popen(f"git -C '{p}' remote get-url origin 2>/dev/null").read().strip()
            m = re.search(r"github\.com[:/]+([^/]+)/([^/\s]+?)(?:\.git)?$", url)
            if m: paths[f"{m.group(1)}/{m.group(2)}".lower()] = p
    results = []
    for r in wanted:
        p = paths.get(r.lower()); row = inv.get(r.lower())
        if not p or not row: results.append({"repo": r, "status": "no-clone-or-inventory"}); continue
        public = row[1] == "PUBLIC"; user_account = os.environ.get("USER_ACCOUNT_OWNERS", "").split(",").count(r.split("/")[0]) > 0  # owners that are User accounts (no org default SECURITY.md)
        results.extend(rollout([p], make_apply(r, public, user_account, "self-hosted", a.only), branch=("ci/fleet-actions-wave2-" + a.only if a.only else "ci/fleet-actions-wave2"),
                               title=("ci: " + a.only + " via fleet-actions@v2.0.0 (advisory)") if a.only else "ci: fleet-check (+ pvr-check / SECURITY.md for public repos) via fleet-actions@v2.0.0",
                               body=BODY, merge=not a.no_merge, dry=a.dry, tel=False))
    for r in results: print(json.dumps({k: r.get(k) for k in ("repo", "status", "files", "detail", "pr")}))
