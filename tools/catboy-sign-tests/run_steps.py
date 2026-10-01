#!/usr/bin/env python3
"""Run catboy-sign.yml's own step scripts against fixtures, the way GitHub runs them
(bash --noprofile --norc -eo pipefail). No CA, no network: covers the gates and checks
that the workflow-level fixtures never reach.

  python3 tools/catboy-sign-tests/run_steps.py      (CI runs it)
"""
import os, shutil, stat, subprocess, sys, tempfile
import yaml

WF = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", ".github", "workflows", "catboy-sign.yml")
steps = {}
for job in yaml.safe_load(open(WF))["jobs"].values():
    for st in job.get("steps", []):
        key = st.get("id") or st.get("name")
        if "run" in st:
            steps[key] = st["run"]
GATE0, RECORD, ASSERT, REPORT = steps["gate0"], steps["Record the input set"], steps["assert"], steps["report"]
results = []


def run(script, env, cwd):
    f = os.path.join(cwd, "step.sh")
    open(f, "w").write(script)
    e = {"PATH": env.pop("PATH", os.environ["PATH"]), "HOME": cwd}
    e.update(env)
    return subprocess.run(["bash", "--noprofile", "--norc", "-eo", "pipefail", f], cwd=cwd, env=e,
                          capture_output=True, text=True)


def outputs(path):
    d = {}
    for line in open(path).read().splitlines():
        if "=" in line:
            k, v = line.split("=", 1); d[k] = v
    return d


def check(name, ok, detail=""):
    results.append(ok); print(("PASS " if ok else "FAIL ") + name + ("" if ok else f": {detail}"))


def ws():
    d = tempfile.mkdtemp(); os.makedirs(os.path.join(d, "tmp")); os.makedirs(os.path.join(d, "dist"))
    open(os.path.join(d, "out"), "w").close(); open(os.path.join(d, "summary"), "w").close()
    return d


def fake_bin(d, version_line):
    b = os.path.join(d, "bin"); os.makedirs(b, exist_ok=True)
    if version_line is not None:
        p = os.path.join(b, "osslsigncode")
        open(p, "w").write(f"#!/bin/sh\necho '{version_line}'\n")
        os.chmod(p, os.stat(p).st_mode | stat.S_IEXEC)
    # a PATH where osslsigncode resolves ONLY to the fake (or not at all)
    path = [b]
    for t in ("bash", "sh", "mkdir", "grep", "head", "sort", "cut", "printf", "cat", "curl", "jq", "openssl", "tr", "wc", "find", "env", "date", "sed"):
        w = shutil.which(t)
        if w:
            os.symlink(w, os.path.join(b, t)) if not os.path.exists(os.path.join(b, t)) else None
    return ":".join(path)


# --- gate0: osslsigncode presence + version floor (must gate, never kill the step) ---
for name, line, want_gate in [("garbage --version output", "no version here", "3"),
                              ("osslsigncode 2.8", "osslsigncode 2.8, using:", "3"),
                              ("osslsigncode 2.14", "osslsigncode 2.14, using:", ""),
                              ("osslsigncode missing", None, "3")]:
    d = ws()
    env = {"PATH": fake_bin(d, line), "GITHUB_OUTPUT": f"{d}/out", "RUNNER_TEMP": f"{d}/tmp", "CURL_HOME": f"{d}/tmp",
           "FETCH_OUTCOME": "success", "ARTIFACT_NAME": "x", "CATBOY_BAO_ADDR": "https://bao.invalid",
           "CATBOY_RUNNER_ISSUER": "test", "GITHUB_REF": "refs/tags/v1.0.0", "CATBOY_SIGN_VERSION": "test", "RUNNER_NAME": "fixture"}
    r = run(GATE0, env, d); o = outputs(f"{d}/out")
    check(f"gate0 {name}: exit 0, gate={want_gate or '(none)'}", r.returncode == 0 and o.get("gate", "MISSING") == want_gate,
          f"rc={r.returncode} out={o} err={r.stderr[-300:]}")
    shutil.rmtree(d)


# --- record + assert ---
def assert_case(name, inputs, mutate, want_rc):
    d = ws()
    for rel in inputs:
        p = os.path.join(d, "dist", rel); os.makedirs(os.path.dirname(p), exist_ok=True); open(p, "w").write("x")
    env = {"RUNNER_TEMP": f"{d}/tmp", "GITHUB_OUTPUT": f"{d}/out"}
    r = run(RECORD, dict(env), d)
    if r.returncode != 0:
        check(f"record ({name})", False, r.stderr); return
    mutate(os.path.join(d, "dist"))
    r = run(ASSERT, dict(env, ARCHIVE_GLOB="*.zip *.tar.gz"), d)
    check(f"assert {name}: exit {want_rc}", r.returncode == want_rc, f"rc={r.returncode} {r.stdout[-300:]}")
    shutil.rmtree(d)

def touch(*names):
    return lambda dist: [open(os.path.join(dist, n), "w").close() for n in names]

IN = ["app.exe", "sub/tool.zip"]
assert_case("unchanged", IN, lambda dist: None, 0)
assert_case("+SIGNATURES.md +archive .p7s/.tsr", IN, touch("SIGNATURES.md", "sub/tool.zip.p7s", "sub/tool.zip.p7s.tsr"), 0)
assert_case("leftover file", IN, touch("old.exe"), 1)
assert_case("missing input", IN, lambda dist: os.remove(os.path.join(dist, "app.exe")), 1)
assert_case("orphan .p7s (no such input)", IN, touch("ghost.zip.p7s"), 1)
assert_case(".p7s for a PE input (signed in place)", IN, touch("app.exe.p7s"), 1)
assert_case(".tsr without its .p7s", IN, touch("sub/tool.zip.p7s.tsr"), 1)
assert_case("injected symlink to a file", IN, lambda dist: os.symlink("/etc/hostname", os.path.join(dist, "evil")), 1)
assert_case("injected symlink to a dir", IN, lambda dist: os.symlink("/etc", os.path.join(dist, "etcdir")), 1)
assert_case("input name with a newline, unchanged", ["we\nird.exe", "a.zip"], lambda dist: None, 0)


# --- report: a failed assert means signed=false, gate=6, red even when not strict ---
for name, assert_outcome, want_rc, want_signed, want_gate in [("assert failed", "failure", 1, "false", "6"),
                                                              ("assert ok", "success", 0, "true", "")]:
    d = ws()
    env = {"GITHUB_OUTPUT": f"{d}/out", "GITHUB_STEP_SUMMARY": f"{d}/summary", "RUNNER_TEMP": f"{d}/tmp", "CURL_HOME": f"{d}/tmp",
           "G0_GATE": "", "G0_REASON": "", "LOGIN_OUTCOME": "success", "CERT_OUTCOME": "success", "CERT_REASON": "",
           "CERT_SERIAL": "01", "HIERARCHY": "staging", "ROOT_FP": "ab", "SIGN_OUTCOME": "success", "SIGN_GATE": "",
           "SIGN_COUNTS": "1/0/0", "UPLOAD_OUTCOME": "skipped" if assert_outcome == "failure" else "success",
           "ASSERT_OUTCOME": assert_outcome, "ARTIFACT_NAME": "x", "STRICT": "false",
           "GITHUB_REPOSITORY": "o/r", "GITHUB_RUN_ID": "1"}
    r = run(REPORT, env, d); o = outputs(f"{d}/out")
    check(f"report {name}: exit {want_rc}, signed={want_signed}, gate={want_gate or '(none)'}",
          r.returncode == want_rc and o.get("signed") == want_signed and o.get("gate", "") == want_gate,
          f"rc={r.returncode} out={o} err={r.stderr[-200:]}")
    shutil.rmtree(d)

sys.exit(0 if all(results) else 1)
