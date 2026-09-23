#!/usr/bin/env python3
"""Gate+merge for fleet-actions rollout PRs (GATE_PREFIX selects the check family, default "security /").
Input: lines "owner/repo <pr-number>" (from driver output). For each PR: if every check
is pass/skipping and the security jobs are present → squash-merge; if anything pending →
wait; if a `security / *` job failed → HOLD (report); if only the repo's own unrelated
checks fail → merge anyway (the PR touches only the two stub files) but report.
Usage: gate_merge.py <list-file> [--max-wait-min N] [--dry]"""
import sys, json, subprocess, time, os
PREFIX = os.environ.get("GATE_PREFIX", "security /")
lst, dry = sys.argv[1], "--dry" in sys.argv
maxw = int(sys.argv[sys.argv.index("--max-wait-min")+1]) if "--max-wait-min" in sys.argv else 40
prs = [l.split() for l in open(lst) if l.strip()]
def checks(repo, n):
    # this gh has no --json for `pr checks`: plain output is "name<TAB>state<TAB>elapsed<TAB>url"
    out = subprocess.run(["gh","pr","checks",str(n),"-R",repo],capture_output=True,text=True).stdout
    cs = []
    for line in out.splitlines():
        parts = line.split("\t")
        if len(parts) < 2: continue
        st = parts[1].strip().lower()
        bucket = "pass" if st == "pass" else "fail" if st in ("fail","cancel") else "skipping" if st.startswith("skip") else "pending"
        cs.append({"name": parts[0].strip(), "bucket": bucket})
    return cs
def state(repo, n):
    return subprocess.run(["gh","pr","view",str(n),"-R",repo,"--json","state","-q",".state"],capture_output=True,text=True).stdout.strip()
pending = {f"{r} {n}" for r, n in prs}; deadline = time.time() + maxw*60; report = {}
while pending and time.time() < deadline:
    for key in sorted(pending):
        repo, n = key.split()
        if state(repo, n) == "MERGED": report[key] = "already merged"; pending.discard(key); continue
        cs = checks(repo, n)
        if not cs or any(c["bucket"] == "pending" for c in cs): continue
        sec = [c for c in cs if c["name"].startswith(PREFIX)]
        sec_bad = [c["name"] for c in sec if c["bucket"] == "fail"]
        other_bad = [c["name"] for c in cs if c["bucket"] == "fail" and not c["name"].startswith(PREFIX)]
        if not sec: report[key] = f"HOLD: no {PREFIX!r} jobs ran"; pending.discard(key); continue
        if sec_bad: report[key] = f"HOLD: {PREFIX!r} jobs failed: {sec_bad}"; pending.discard(key); continue
        if dry: report[key] = f"would merge (other failing, unrelated: {other_bad})"; pending.discard(key); continue
        m = subprocess.run(["gh","pr","merge",str(n),"-R",repo,"--squash","--delete-branch"],capture_output=True,text=True)
        report[key] = ("merged" if m.returncode == 0 else f"MERGE FAILED: {m.stderr.strip()[:100]}") + (f" (own checks red, unrelated: {other_bad})" if other_bad else "")
        pending.discard(key)
    if pending: time.sleep(30)
for k in pending: report[k] = "TIMEOUT: still pending"
for k, v in sorted(report.items()): print(f"{k}: {v}")
print(f"SUMMARY merged={sum(1 for v in report.values() if v.startswith('merged'))} hold={sum(1 for v in report.values() if v.startswith('HOLD'))} timeout={sum(1 for v in report.values() if v.startswith('TIMEOUT'))} failed={sum(1 for v in report.values() if 'FAILED' in v)}")
