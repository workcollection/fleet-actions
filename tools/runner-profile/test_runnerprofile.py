"""runner-LXC profile: sampling, sizing rules, pct lines and rollback on a fake cgroup + pve tree.
Run: python3 tools/runner-profile/test_runnerprofile.py"""
import os, sys, tempfile

T = tempfile.mkdtemp(prefix="rprof-")
os.environ.update(RUNNERPROFILE_CG_ROOT=os.path.join(T, "cg"), RUNNERPROFILE_PVE_CONF=os.path.join(T, "pve"),
                  RUNNERPROFILE_LOG_DIR=os.path.join(T, "log"), RUNNERPROFILE_MEMINFO=os.path.join(T, "meminfo"),
                  RUNNERPROFILE_HOST_IO_PSI=os.path.join(T, "io"))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import runnerprofile as R  # noqa: E402

fails = []


def check(name, ok, detail=""):
    print(("ok  " if ok else "FAIL"), name, "" if ok else detail)
    fails.append(name) if not ok else None


CG = os.path.join(T, "cg", "103")
os.makedirs(CG)
os.makedirs(os.path.join(T, "pve"))
CONF = """arch: amd64
cores: 8
cpulimit: 4
cpuunits: 10
memory: 4096
swap: 2048
lxc.cgroup2.io.bfq.weight: 10

[before-upgrade]
memory: 2048
"""
open(os.path.join(T, "pve", "103.conf"), "w").write(CONF)
open(os.path.join(T, "meminfo"), "w").write("MemTotal: 61000000 kB\nMemAvailable: 17000000 kB\n")
open(os.path.join(T, "io"), "w").write("some avg10=1 avg60=2.0 avg300=1 total=1\nfull avg10=1 avg60=1.5 avg300=1 total=1\n")


def cg(refault, high, anon_gb, usage_s, throttled_s, rbytes, swap_mb=300):
    w = lambda n, v: open(os.path.join(CG, n), "w").write(v)
    w("memory.stat", f"anon {int(anon_gb * R.GiB)}\nfile 236191744\nworkingset_refault_file {refault}\npgmajfault 23500308\n")
    w("memory.events", f"low 0\nhigh {high}\nmax 15147\noom 307\noom_kill 1\n")
    w("cpu.stat", f"usage_usec {int(usage_s * 1e6)}\nuser_usec 1\nsystem_usec 1\nthrottled_usec {int(throttled_s * 1e6)}\n")
    w("io.stat", f"259:0 rbytes={rbytes} wbytes=1000 rios=1 wios=1 dbytes=0 dios=0\n252:2 rbytes=0 wbytes=0\n")
    w("memory.current", str(4 * R.GiB))
    w("memory.max", str(4 * R.GiB))
    w("memory.swap.current", str(swap_mb * 2 ** 20))
    w("memory.pressure", "some avg10=0.00 avg60=3.10 avg300=0 total=1\nfull avg10=0 avg60=0 avg300=0 total=1\n")
    w("io.pressure", "some avg10=0.00 avg60=9.00 avg300=0 total=1\nfull avg10=0 avg60=0 avg300=0 total=1\n")


cg(460_000_000, 6_580_000, 0.3, 1000, 10, 2_000_000_000_000)
s = R.sample(103)
check("pve conf: live section only (snapshot ignored), io weight read",
      s["conf"] == {"cores": 8, "cpulimit": 4.0, "cpuunits": 10, "memory": 4096, "swap": 2048, "io_weight": 10}, s["conf"])
check("sample reads refaults, events, io bytes of the busiest device (dm + disk not double-counted), host io full, available memory",
      s["refault_file"] == 460_000_000 and s["high"] == 6_580_000 and s["io_rbytes"] == 2_000_000_000_000
      and s["host_io_full"] == 1.5 and s["host_mem_available_kb"] == 17_000_000, s)

# thrash day (the CT103 shape before 2026-10-28): +12 M refault pages (~46 GB) and +470 k high events in 1 day
rows = [dict(s, ts=0), dict(s, ts=86400, refault_file=s["refault_file"] + 12_000_000, high=s["high"] + 470_000,
                             cpu_usage_usec=s["cpu_usage_usec"] + int(86400 * 2e6), io_rbytes=s["io_rbytes"] + 160 * R.GiB)]
t = R.tune(rows)
p = t["proposal"]
check("thrash doubles memory with the measured reason", p.get("memory", {}).get("value") == 8192
      and "refaults" in p["memory"]["reason"], t)
check("swap above 1 G is cut to 1 G", p.get("swap", {}).get("value") == 1024, p)
check("io weight stays low while it thrashes", "io_weight" not in p, p)
check("cpu not proposed when not throttled", "cpulimit" not in p, p)
lines = R.pct_lines(103, {k: v["value"] for k, v in p.items()}, t["current"])
check("pct lines: backup first, then one pct set", lines[0].startswith("cp /etc/pve/lxc/103.conf")
      and lines[1] == "pct set 103 --memory 8192 --swap 1024", lines)

# the host cannot spare it: the raise is capped at available minus the reserve
lowmem = [dict(r, host_mem_available_kb=6 * 1024 ** 2) for r in rows]
check("memory raise capped by host available minus 4 G reserve",
      R.tune(lowmem)["proposal"]["memory"]["value"] == 4096 + 2048, R.tune(lowmem)["proposal"])

# healthy after the fix: no thrash, low host io -> io weight may go back up; nothing else
ok = [dict(r, conf=dict(r["conf"], memory=8192, swap=1024)) for r in rows]
ok[1] = dict(ok[1], refault_file=ok[0]["refault_file"] + 50_000, high=ok[0]["high"] + 10)
tp = R.tune(ok)["proposal"]
check("healthy: only io weight 10 -> 50 (host io full p95 1.5 %)", tp.get("io_weight", {}).get("value") == 50
      and set(tp) == {"io_weight"}, tp)
iol = R.pct_lines(103, {"io_weight": 50}, {"io_weight": 10})
check("io weight is a conf line + a live write, not a pct flag",
      any("1i lxc.cgroup2.io.bfq.weight: 50" in x for x in iol) and any("io.bfq.weight" in x and "echo 50" in x for x in iol)
      and not any(x.startswith("pct set") for x in iol), iol)

# cpu-bound: p95 at the limit and > 25 % throttled
cpu = [dict(r) for r in ok]
cpu[1] = dict(cpu[1], cpu_usage_usec=cpu[0]["cpu_usage_usec"] + int(86400 * 4e6),
              cpu_throttled_usec=cpu[0]["cpu_throttled_usec"] + int(86400 * 1.5e6))
check("cpu-bound raises cpulimit by one, below cores", R.tune(cpu)["proposal"].get("cpulimit", {}).get("value") == 5,
      R.tune(cpu)["proposal"])
check("a restarted CT (counter went down) does not count negative", R._rate_per_day(
    [{"ts": 0, "x": 100}, {"ts": 43200, "x": 150}, {"ts": 86400, "x": 20}], "x") == 70)
check("too few samples is an error, not a guess", "error" in R.tune(rows[:1]))

# measure -> log -> tune via the CLI; create; rollback from the newest backup
R.measure(103)
check("measure appends to the per-CT log", len(R.load(103, 1)) == 1)
open(os.path.join(T, "pve", "103.conf.bak-202610281500"), "w").write(CONF.replace("memory: 4096", "memory: 4096"))
open(os.path.join(T, "pve", "103.conf"), "w").write(CONF.replace("memory: 4096", "memory: 8192").replace("swap: 2048", "swap: 1024")
                                                       .replace("io.bfq.weight: 10", "io.bfq.weight: 50"))
b, rl = R.rollback_lines(103)
check("rollback restores memory, swap and io weight from the newest backup",
      b.endswith("bak-202610281500") and "pct set 103 --memory 4096 --swap 2048" in rl
      and any("bfq.weight: 10" in x for x in rl), rl)
check("create prints only what differs from the default profile", R.pct_lines(103, R.DEFAULT_PROFILE, R.pve_conf(103))
      == R.pct_lines(103, {"io_weight": 10}, {"io_weight": 50}), R.pct_lines(103, R.DEFAULT_PROFILE, R.pve_conf(103)))
print("FAILED:" if fails else "all ok", ", ".join(fails))
sys.exit(1 if fails else 0)
