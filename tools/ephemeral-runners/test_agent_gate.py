"""pn-agent start gate: GATE_POLICY=load keeps the old rule; GATE_POLICY=psi asks tools/n4-ci-gate/gate_policy.py; a
broken or missing policy module falls back to the load rule (never a silent open gate). Run: python3 this_file"""
import importlib.util, os, sys, tempfile
HERE = os.path.dirname(os.path.abspath(__file__))
spec = importlib.util.spec_from_file_location("pn_agent", os.path.join(HERE, "pn-agent.py"))
A = importlib.util.module_from_spec(spec); spec.loader.exec_module(A)
fails, logs = [], []
A.log = lambda m: logs.append(m)


def check(name, ok, detail=""):
    print(("ok  " if ok else "FAIL"), name, "" if ok else detail)
    fails.append(name) if not ok else None


T = tempfile.mkdtemp(prefix="pngate-")
policy_dir = os.path.join(os.path.dirname(HERE), "n4-ci-gate")
psi, temp = os.path.join(T, "psi"), os.path.join(T, "temp")
os.environ["N4_GATE_CONF"] = os.path.join(T, "gate.conf")
open(os.environ["N4_GATE_CONF"], "w").write(f"PSI_FILE={psi}\nTEMP_FILE={temp}\n")


def world(psi_avg60, temp_c, load):
    open(psi, "w").write(f"some avg10=1.0 avg60={psi_avg60} avg300=1.0 total=1\n")
    open(temp, "w").write(f"{int(temp_c * 1000)}\n")
    A.load1 = lambda: load


base = dict(A.DEFAULTS, GATE_POLICY_DIR=policy_dir)
world(20.0, 60, 30.0)
check("load policy (default): load 30 > 8 refuses, as before", A.start_gate(dict(base), 0)[0] is False)
check("psi policy: load 30 but PSI 20 % and cool -> start", A.start_gate(dict(base, GATE_POLICY="psi"), 1)[0] is True)
world(55.0, 60, 30.0)
check("psi policy: PSI 55 % with a runner already up -> refuse",
      A.start_gate(dict(base, GATE_POLICY="psi"), 1)[0] is False)
check("psi policy: PSI 55 % and the pool idle -> the guaranteed minimum starts",
      A.start_gate(dict(base, GATE_POLICY="psi"), 0)[0] is True)
world(5.0, 88, 1.0)
check("psi policy: thermal stop wins (88 C)", A.start_gate(dict(base, GATE_POLICY="psi"), 0)[0] is False)
world(5.0, 60, 30.0)
logs.clear()
ok, why = A.start_gate(dict(base, GATE_POLICY="psi", GATE_POLICY_DIR=os.path.join(T, "nowhere")), 0)
if "gate_policy" in sys.modules:   # the module was imported above from the real dir: simulate a missing one
    del sys.modules["gate_policy"]
    sys.path[:] = [p for p in sys.path if p != policy_dir]
    ok, why = A.start_gate(dict(base, GATE_POLICY="psi", GATE_POLICY_DIR=os.path.join(T, "nowhere")), 0)
check("psi policy without the module: the load rule decides and the log says so",
      ok is False and "load 30.00 > 8" in why and any("falling back to the load rule" in m for m in logs), (ok, why, logs))
print("FAILED:" if fails else "all ok", ", ".join(fails))
sys.exit(1 if fails else 0)
