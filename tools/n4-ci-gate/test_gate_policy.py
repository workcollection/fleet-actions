"""n4 CI gate policy + shadow summary. Run: python3 tools/n4-ci-gate/test_gate_policy.py"""
import json, os, sys, tempfile, time
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import gate_policy as G  # noqa: E402
import importlib.util  # noqa: E402
spec = importlib.util.spec_from_file_location("shadow", os.path.join(os.path.dirname(os.path.abspath(__file__)), "n4-gate-shadow.py"))
S = importlib.util.module_from_spec(spec); spec.loader.exec_module(S)
fails = []


def check(name, ok, detail=""):
    print(("ok  " if ok else "FAIL"), name, "" if ok else detail)
    fails.append(name) if not ok else None


check("low pressure, cool: start", G.decide(2, 20.0, 60.0)[0])
check("high pressure, pool busy: refuse", not G.decide(1, 45.0, 60.0)[0])
check("guaranteed minimum: idle pool starts at 45 % (today's 20-39 % band and above)", G.decide(0, 45.0, 60.0)[0])
check("saturated box: even the minimum waits at 75 %", not G.decide(0, 75.0, 60.0)[0])
check("thermal is the hard stop, also for the minimum", not G.decide(0, 5.0, 86.0)[0]
      and "thermal" in G.decide(0, 5.0, 86.0)[1])
check("stale inputs refuse with the reason", not G.decide(0, None, 60.0)[0] and not G.decide(0, 5.0, None)[0])
T = tempfile.mkdtemp()
c = dict(G.DEFAULTS, PSI_FILE=os.path.join(T, "psi"), TEMP_FILE=os.path.join(T, "temp"))
open(c["PSI_FILE"], "w").write("some avg10=33.10 avg60=38.78 avg300=36.99 total=1\nfull avg10=0 avg60=0 avg300=0 total=0\n")
open(c["TEMP_FILE"], "w").write("61000\n")
check("reads host PSI avg60 and millidegree temperature", G.read_psi(c) == 38.78 and G.read_temp(c) == 61.0)
old = time.time() - 600
os.utime(c["PSI_FILE"], (old, old))
check("a stale PSI file reads as missing", G.read_psi(c) is None)
L = os.path.join(T, "log.jsonl")
now = int(time.time())
with open(L, "w") as fh:
    for i, (cur, new, psi) in enumerate(((False, True, 30.0), (False, True, 35.0), (True, True, 10.0), (False, False, 80.0))):
        fh.write(json.dumps({"ts": now - 60 * i, "load1": 20, "psi_avg60": psi, "temp_c": 60 + i,
                             "pn": {"current": cur, "new": new, "new_why": "x"},
                             "mari": {"current": cur, "new": new, "new_why": "x"}}) + "\n")
s = S.summary(24, L)
check("summary counts current vs new allow/refuse and the maxima",
      s["ticks"] == 4 and s["pn"]["current_allow"] == 1 and s["pn"]["new_allow"] == 3 and s["pn"]["differ"] == 2
      and s["max_psi_avg60"] == 80.0 and s["max_temp_c"] == 63, s)
print("FAILED:" if fails else "all ok", ", ".join(fails))
sys.exit(1 if fails else 0)
