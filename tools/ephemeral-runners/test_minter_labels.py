#!/usr/bin/env python3
"""Label routing tests for pn-minter.queued_jobs (no network): python3 test_minter_labels.py"""
import importlib.util, os, sys
spec = importlib.util.spec_from_file_location("m", os.path.join(os.path.dirname(__file__), "pn-minter.py"))
m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)

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

def run(conf, labels):
    c = dict(m.DEFAULTS); c.update(conf)
    return [(j["eligible"], j["reason"]) for j in m.queued_jobs(c, FakeGH(labels), "polo-nyan/pawkit")]

SIGN = {"JOB_LABELS_OK": "self-hosted,catboy-sign,polo-nyan-signing", "REQUIRE_LABELS": "catboy-sign"}
cases = [
  ("signing: [self-hosted, linux] is not this pool's", SIGN, [["self-hosted", "linux"]], []),
  ("signing: bare [self-hosted] is UNSAFE",            SIGN, [["self-hosted"]], [(False, "UNSAFE")]),
  ("signing: [self-hosted, catboy-sign] eligible",     SIGN, [["self-hosted", "catboy-sign"]], [(True, None)]),
  ("signing: [catboy-sign] eligible",                   SIGN, [["catboy-sign"]], [(True, None)]),
  ("signing: [catboy-sign, gpu] not eligible",          SIGN, [["catboy-sign", "gpu"]], [(False, "labels")]),
  ("general: [self-hosted, linux] eligible",            {}, [["self-hosted", "linux"]], [(True, None)]),
  ("general: [self-hosted, catboy-sign] refused",       {}, [["self-hosted", "catboy-sign"]], [(False, "labels")]),
  ("general: [ubuntu-latest] ignored",                  {}, [["ubuntu-latest"]], []),
]
fail = 0
for name, conf, labels, want in cases:
    got = run(conf, labels)
    ok = len(got) == len(want) and all(g[0] == w[0] and (w[1] is None and g[1] is None or (g[1] or "").startswith(w[1] or "~"))
                                       for g, w in zip(got, want))
    print(("PASS " if ok else "FAIL ") + name + ("" if ok else f": got {got}, want {want}"))
    fail |= not ok
sys.exit(fail)
