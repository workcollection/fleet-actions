#!/usr/bin/env python3
"""Label routing, repo allowlist and EXTRA_RUN_ARGS tests (no network, no docker).
Run: python3 tools/ephemeral-runners/test_minter_labels.py   (CI runs it)"""
import importlib.util, os, sys
here = os.path.dirname(os.path.abspath(__file__))
def load(name, file):
    spec = importlib.util.spec_from_file_location(name, os.path.join(here, file))
    mod = importlib.util.module_from_spec(spec); spec.loader.exec_module(mod); return mod
m = load("minter", "pn-minter.py")
a = load("agent", "pn-agent.py")

class FakeGH:
    def __init__(self, jobs): self.jobs = jobs
    def call(self, method, path, cache=False):
        if "/runs?status=queued" in path:
            return {"workflow_runs": [{"id": 1, "path": ".github/workflows/x.yml", "event": "push",
                                      "actor": {"login": "polo-nyan"}, "head_repository": {"full_name": "polo-nyan/pawkit"}}]}
        if "/runs?status=in_progress" in path:
            return {"workflow_runs": []}
        return {"jobs": [{"id": i, "name": f"j{i}", "status": "queued", "labels": l, "created_at": "2026-10-01T00:00:00Z"}
                         for i, l in enumerate(self.jobs)]}

def route(conf, labels):
    c = dict(m.DEFAULTS); c.update(conf)
    return [(j["eligible"], j["reason"]) for j in m.queued_jobs(c, FakeGH(labels), "polo-nyan/pawkit")]

# The signing pool as deployed: exactly one unique label, required, repo allowlist.
SIGN = {"JOB_LABELS_OK": "polo-nyan-signing", "REQUIRE_LABELS": "polo-nyan-signing", "ONLY_REPOS": "polo-nyan/pawkit"}
GEN = {}
results = []
def check(name, got, want):
    ok = got == want if not callable(want) else want(got)
    results.append(ok); print(("PASS " if ok else "FAIL ") + name + ("" if ok else f": got {got}"))

def eligible(got): return got == [(True, None)]
def refused(prefix): return lambda got: len(got) == 1 and not got[0][0] and (got[0][1] or "").startswith(prefix)

check("signing: [polo-nyan-signing] eligible",                 route(SIGN, [["polo-nyan-signing"]]), eligible)
check("signing: [self-hosted, polo-nyan-signing] NOT eligible", route(SIGN, [["self-hosted", "polo-nyan-signing"]]), refused("labels"))
check("signing: [polo-nyan-signing, gpu] NOT eligible",        route(SIGN, [["polo-nyan-signing", "gpu"]]), refused("labels"))
check("signing: [self-hosted] is not this pool's",             route(SIGN, [["self-hosted"]]), [])
check("signing: [self-hosted, linux] is not this pool's",      route(SIGN, [["self-hosted", "linux"]]), [])
check("signing: [ubuntu-latest] ignored",                      route(SIGN, [["ubuntu-latest"]]), [])
c = dict(m.DEFAULTS); c.update(SIGN)
check("signing: allowlisted repo served",   m.served_repos(c, ["polo-nyan/pawkit", "polo-nyan/other"]), ["polo-nyan/pawkit"])
check("signing: wrong repo NOT served",     m.served_repos(c, ["polo-nyan/other"]), [])
check("general: [self-hosted, linux] eligible",          route(GEN, [["self-hosted", "linux"]]), eligible)
check("general: [polo-nyan-signing] ignored",            route(GEN, [["polo-nyan-signing"]]), [])
check("general: [self-hosted, polo-nyan-signing] refused", route(GEN, [["self-hosted", "polo-nyan-signing"]]), refused("labels"))
cg = dict(m.DEFAULTS)
check("general: no allowlist serves every repo", m.served_repos(cg, ["a/x", "a/y"]), ["a/x", "a/y"])

def args_ok(raw):
    try: a.extra_run_args(raw); return True
    except ValueError: return False
check("agent: --env-file + ro mount accepted", args_ok("--env-file /etc/catboy/runner.env -v /etc/catboy/runner:/run/catboy:ro"), True)
check("agent: empty accepted",                 args_ok(""), True)
check("agent: rw mount refused",               args_ok("-v /etc/catboy/runner:/run/catboy"), False)
check("agent: docker.sock refused",            args_ok("-v /var/run/docker.sock:/var/run/docker.sock:ro"), False)
check("agent: --privileged refused",           args_ok("--privileged --env-file /x"), False)
check("agent: relative path refused",          args_ok("--env-file runner.env"), False)
check("agent: --network host refused",         args_ok("--network host"), False)
sys.exit(0 if all(results) else 1)
