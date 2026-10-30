"""pn-agent `status` capacity block (runner router phase 0): slots = min(free job slots, memory slots), 0 on drain,
backup window, low disk or a shut start gate; a broken capacity input never breaks `status`. Run: python3 this_file"""
import importlib.util, io, json, os, sys, contextlib, time
HERE = os.path.dirname(os.path.abspath(__file__))
spec = importlib.util.spec_from_file_location("pn_agent", os.path.join(HERE, "pn-agent.py"))
A = importlib.util.module_from_spec(spec); spec.loader.exec_module(A)
fails = []


def check(name, ok, detail=""):
    print(("ok  " if ok else "FAIL"), name, "" if ok else detail)
    fails.append(name) if not ok else None


base = dict(A.DEFAULTS, HOST_NAME="n4", MAX_JOBS="3", MEMORY="3g", MEM_RESERVE_GB="2", DISK_FREE_MIN_GB="30")
A.disk_free_gb = lambda path="/var/lib/docker": 100.0
A.mem_available_gb = lambda: 20.0

cap = A.capacity(base, 1, True, "open")
check("free slots = MAX_JOBS - live when memory is plenty", cap["slots_free"] == 2 and cap["admit"] == 1, cap)
check("host name and labels reported", cap["host"] == "n4" and cap["labels"] == ["self-hosted", "linux", "polo-nyan"], cap)

A.mem_available_gb = lambda: 6.5   # (6.5 - 2) // 3 = 1
cap = A.capacity(base, 0, True, "open")
check("memory caps the slots", cap["mem_slots"] == 1 and cap["slots_free"] == 1, cap)
A.mem_available_gb = lambda: 4.0   # (4 - 2) // 3 = 0
cap = A.capacity(base, 0, True, "open")
check("no memory slot -> 0 slots, admit 0", cap["slots_free"] == 0 and cap["admit"] == 0 and cap["stops"] == [], cap)
A.mem_available_gb = lambda: 20.0

cap = A.capacity(dict(base, DRAIN="1"), 0, True, "open")
check("drain -> 0 slots", cap["slots_free"] == 0 and cap["stops"] == ["drain"], cap)

cap = A.capacity(base, 0, False, "psi gate: cpu pressure 55 %")
check("shut start gate -> 0 slots, reason kept", cap["slots_free"] == 0 and "psi gate" in cap["stops"][0], cap)

A.disk_free_gb = lambda path="/var/lib/docker": 10.0
cap = A.capacity(base, 0, True, "open")
check("low disk -> 0 slots", cap["slots_free"] == 0 and cap["stops"][0].startswith("disk free"), cap)
A.disk_free_gb = lambda path="/var/lib/docker": 100.0

noon = time.mktime((2026, 10, 30, 12, 40, 0, 0, 0, -1))
check("inside window", A.in_window("12:30-13:30", noon))
check("outside window", not A.in_window("13:00-14:00", noon))
check("window wrapping midnight", A.in_window("23:00-01:00", time.mktime((2026, 10, 30, 0, 30, 0, 0, 0, -1))))
check("empty window = never", not A.in_window("", noon))

check("docker memory sizes", [A.docker_mem_gb(x) for x in ("3g", "512m", "3072M")] == [3.0, 0.5, 3.0])

# status: existing keys unchanged, capacity added; a broken capacity input yields an error block, not a crash
A.managed = lambda: []
A.start_gate = lambda c, running: (True, "open")
A.load1 = lambda: 1.0
A.mem_available_gb = lambda: (_ for _ in ()).throw(ValueError("no meminfo"))
out = io.StringIO()
with contextlib.redirect_stdout(out):
    rc = A.cmd_status(base)
st = json.loads(out.getvalue())
check("status keeps the minter's keys", rc == 0 and {"load1", "disk_free_gb", "max_jobs", "containers", "gate"} <= set(st), st)
check("broken capacity input -> error block with 0 slots",
      st["capacity"].get("slots_free") == 0 and "no meminfo" in st["capacity"].get("error", ""), st)

print("FAILED:" if fails else "all ok", ", ".join(fails))
sys.exit(1 if fails else 0)
