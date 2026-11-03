#!/usr/bin/env python3
"""Runner router phase 1a in pn-minter: host registry (absent = AGENT_CMD exactly as before), shadow
placement, API budget steps, tick log + heartbeat, and one full tick end to end (no network, no docker).
Run: python3 tools/ephemeral-runners/test_minter_router.py   (CI runs it)"""
import importlib.util, io, json, os, sys, tempfile, time, contextlib, urllib.error
here = os.path.dirname(os.path.abspath(__file__))
spec = importlib.util.spec_from_file_location("minter", os.path.join(here, "pn-minter.py"))
m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
tmp = tempfile.mkdtemp()
fails = []


def check(name, ok, detail=""):
    print(("ok  " if ok else "FAIL"), name, "" if ok else detail)
    fails.append(name) if not ok else None


def conf(**kw):
    c = dict(m.DEFAULTS, AGENT_CMD="ssh root@n4 pn-agent", STATE=os.path.join(tmp, "state.json")); c.update(kw)
    return c


def write(name, text):
    p = os.path.join(tmp, name)
    open(p, "w").write(text)
    return p


# --- registry ---------------------------------------------------------------------------------------
default = [{"name": "default", "cmd": "ssh root@n4 pn-agent", "weight": 1.0, "drain": False}]
check("no HOSTS_FILE -> AGENT_CMD", m.hosts(conf()) == default, m.hosts(conf()))
check("HOSTS_FILE set but absent -> AGENT_CMD", m.hosts(conf(HOSTS_FILE=os.path.join(tmp, "nope"))) == default)
hf = write("hosts.conf", '# registry\nname=n4 cmd="ssh -o BatchMode=yes root@n4 pct exec 113 -- pn-agent"\n\n'
                         'name=pve cmd="ssh root@pve pn-agent" weight=0.2 drain=1\n')
hs = m.hosts(conf(HOSTS_FILE=hf))
check("two lines parsed, quoted cmd kept whole",
      [h["name"] for h in hs] == ["n4", "pve"] and hs[0]["cmd"].endswith("pct exec 113 -- pn-agent")
      and hs[1]["weight"] == 0.2 and hs[1]["drain"] and not hs[0]["drain"], hs)
for label, text in (("line without cmd", "name=n4\n"), ("duplicate name", 'name=a cmd=x\nname=a cmd=y\n'),
                    ("only comments", "# nothing\n")):
    try:
        m.hosts(conf(HOSTS_FILE=write("bad.conf", text)))
        check(f"bad registry raises: {label}", False)
    except ValueError:
        check(f"bad registry raises: {label}", True)

# --- shadow placement -------------------------------------------------------------------------------
H = [{"name": "n4", "weight": 1.0, "drain": False}, {"name": "pve", "weight": 0.2, "drain": False}]
st = lambda free, running=0: {"max_jobs": 3, "containers": [{"state": "running"}] * running,
                              "capacity": {"slots_free": free}, "gate": {"allow": True}}
jobs = [{"repo": "o/r", "job": i} for i in range(4)]
views = [m.host_view(H[0], st(2)), m.host_view(H[1], st(1))]
check("heavier host first, then the next, then none",
      [p["host"] for p in m.place(H, views, jobs)] == ["n4", "n4", "pve", None])
views = [m.host_view(dict(H[0], drain=True), st(3)), m.host_view(H[1], None)]
check("drained and unreachable hosts get nothing",
      [p["host"] for p in m.place(H, views, jobs[:1])] == [None] and views[1]["state"] == "unreachable")
E = [{"name": "a", "weight": 1.0, "drain": False}, {"name": "b", "weight": 1.0, "drain": False}]
views = [m.host_view(E[0], st(3, running=2)), m.host_view(E[1], st(3, running=0))]
check("equal weight -> fewest running", [p["host"] for p in m.place(E, views, jobs[:3])] == ["b", "b", "a"])
check("agent without a capacity block -> 0 slots", m.host_view(H[0], {"max_jobs": 3, "containers": []})["slots_free"] == 0)

# --- API budget -------------------------------------------------------------------------------------
now = 1_800_000_000
api = lambda calls=0, rem=None, reset=now + 600: {"calls": [now - 10] * calls + [now - 4000], "remaining": rem, "reset": reset}
c = conf()
for want, n, a in (("ok", 0, api(rem=None)), ("ok", 0, api(rem=4000)), ("slow", 0, api(rem=1499)),
                   ("lean", 0, api(rem=499)), ("pause", 0, api(rem=199)), ("ok", 0, api(rem=100, reset=now - 1)),
                   ("over", 600, api(calls=600, rem=4000))):
    got = m.budget_step(c, a, now)
    check(f"budget {want} (remaining {a['remaining']}, {n} calls/h)", got == want, got)
a = api(calls=600, rem=4000)
check("API_BUDGET_H=0 -> no own cap", m.budget_step(conf(API_BUDGET_H="0"), a, now) == "ok")
check("calls older than 1 h are pruned", len(a["calls"]) == 600)

class Hdr(dict):
    def get(self, k, d=None): return dict.get(self, k, d)
gh = m.GitHub("t", {})
gh._note(Hdr({"X-RateLimit-Remaining": "4321", "X-RateLimit-Reset": "99"}), True)
gh._note(Hdr({"X-RateLimit-Remaining": "4320"}), False)
check("a 304 is not billable, remaining is still read",
      len(gh.api["calls"]) == 1 and gh.api["remaining"] == 4320, gh.api)

# --- tick log + heartbeat ---------------------------------------------------------------------------
c = conf(TICK_LOG_MAX_MB="0.0002")  # 200 bytes
m.write_tick(c, {"at": 1, "agent_ok": True, "budget": "ok", "queued": {"eligible": 2}, "pad": "x" * 300})
m.write_tick(c, {"at": 2, "agent_ok": True, "budget": "ok", "queued": {"eligible": 0}})
log = os.path.join(tmp, "state.ticks.jsonl")
check("log next to STATE, rotated to .1 over the cap",
      os.path.exists(log + ".1") and [json.loads(x)["at"] for x in open(log)] == [2])
hb = json.load(open(os.path.join(tmp, "state.heartbeat.json")))
check("heartbeat next to STATE, latest tick", hb == {"at": 2, "agent_ok": True, "budget": "ok", "eligible": 0}, hb)
c = conf(STATE=os.path.join(tmp, "signing-state.json"), TICK_LOG="-", HEARTBEAT="-")
m.write_tick(c, {"at": 3})
check("'-' turns both off", not os.path.exists(os.path.join(tmp, "signing-state.ticks.jsonl"))
      and not os.path.exists(os.path.join(tmp, "signing-state.heartbeat.json")))
blocker = write("afile", "")
out = io.StringIO()
with contextlib.redirect_stdout(out):
    m.write_tick(conf(TICK_LOG=os.path.join(blocker, "x.jsonl")), {"at": 4})
check("unwritable log -> a WARN line, no exception", "WARN tick log" in out.getvalue(), out.getvalue())

# --- one tick end to end ----------------------------------------------------------------------------
class FakeGH:
    calls = []
    def __init__(self, token, state):
        self.api = state.setdefault("api", {"calls": [], "remaining": None, "reset": 0})
        self.etags = {}
    def call(self, method, path, cache=False):
        FakeGH.calls.append((method, path))
        self.api["calls"].append(int(time.time()))
        if path.startswith("/user/repos"):
            return [{"owner": {"login": "polo-nyan"}, "full_name": "polo-nyan/pawkit", "private": True, "archived": False}]
        if "/runs?status=queued" in path:
            return {"workflow_runs": [{"id": 1, "path": ".github/workflows/ci.yml", "event": "push",
                                       "actor": {"login": "polo-nyan"}, "head_repository": {"full_name": "polo-nyan/pawkit"}}]}
        if "/runs?status=in_progress" in path:
            return {"workflow_runs": []}
        if "/jobs?" in path:
            return {"jobs": [{"id": 7, "name": "test", "status": "queued", "labels": ["self-hosted", "linux"],
                              "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}]}
        if path.endswith("/registration-token"):
            return {"token": "REG"}
        if path.endswith("/runners?per_page=100"):
            return {"runners": []}
        raise AssertionError(path)

agent_calls = []
def fake_agent(c, sub, stdin=None, cmd=None):
    agent_calls.append((cmd or c["AGENT_CMD"], sub))
    class R: returncode = 0
    R.stdout = json.dumps({"load1": 1.0, "disk_free_gb": 100, "max_jobs": 3, "containers": [],
                           "gate": {"allow": True}, "capacity": {"slots_free": 3, "admit": 1}}) if sub == "status" else "started pn-1\n"
    return R

def run_tick(conf_text, state_name):
    FakeGH.calls.clear(); agent_calls.clear()
    m.CONF_FILE = write(state_name + ".conf", conf_text + f"\nSTATE={os.path.join(tmp, state_name)}.json\n")
    m.GitHub, m.agent = FakeGH, fake_agent
    os.environ["GH_TOKEN"] = "x"
    sys.argv = ["pn-minter"]
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        rc = m.main()
    path = os.path.join(tmp, state_name + ".ticks.jsonl")
    return rc, [json.loads(x) for x in open(path)] if os.path.exists(path) else []

rc, recs = run_tick("AGENT_CMD=ssh root@n4 pn-agent", "legacy")
check("no registry: every agent call goes to AGENT_CMD", rc == 0 and {a[0] for a in agent_calls} == {"ssh root@n4 pn-agent"}
      and [a[1] for a in agent_calls] == ["status", "start"], agent_calls)
r = recs[-1] if recs else {}
check("tick line: placement = what was started, on the one host",
      r.get("placement") == [{"repo": "polo-nyan/pawkit", "job": 7, "host": "default"}]
      and r.get("started") == [{"repo": "polo-nyan/pawkit", "job": 7, "host": "default"}]
      and r.get("queued") == {"eligible": 1, "need": 1, "skipped": 0} and r.get("api", {}).get("billable_1h", 0) >= 5, r)

reg = write("one-host.conf", 'name=n4 cmd="ssh root@n4b pct exec 113 -- pn-agent"\n')
rc, recs = run_tick(f"AGENT_CMD=ssh root@OLD pn-agent\nHOSTS_FILE={reg}\nNOT_VISIBLE=smol-kitten", "reg")
check("registry line replaces AGENT_CMD for status and start",
      {a[0] for a in agent_calls} == {"ssh root@n4b pct exec 113 -- pn-agent"}, agent_calls)
check("smol-kitten logged as not visible, host named n4",
      recs[-1]["not_visible"] == {"smol-kitten": "not visible"} and recs[-1]["started"][0]["host"] == "n4", recs[-1])

st_path = os.path.join(tmp, "over.json")
json.dump({"api": {"calls": [int(time.time())] * 600, "remaining": 4000, "reset": int(time.time()) + 600}}, open(st_path, "w"))
rc, recs = run_tick("AGENT_CMD=ssh root@n4 pn-agent\nNOTIFY_CMD=", "over")
check("over budget: no GitHub call, no start, heartbeat still written",
      rc == 0 and FakeGH.calls == [] and [a[1] for a in agent_calls] == ["status"] and recs[-1]["skipped"] == "over"
      and os.path.exists(os.path.join(tmp, "over.heartbeat.json")), (FakeGH.calls, agent_calls, recs[-1:]))

rc, recs = run_tick(f"AGENT_CMD=x\nHOSTS_FILE={write('broken.conf', 'name=n4')}", "broken")
check("broken registry: exit 1, nothing started, no fallback to AGENT_CMD", rc == 1 and agent_calls == [], agent_calls)

print("FAILED:" if fails else "all ok", ", ".join(fails))
sys.exit(1 if fails else 0)
