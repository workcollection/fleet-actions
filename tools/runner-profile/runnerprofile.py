#!/usr/bin/env python3
"""runner-LXC profile: size a dedicated CI runner LXC from what its jobs measurably use (orchestrator m-8646 (4),
plan pawkit var/plans/runner-lxc-profile.md). Runs on the Proxmox HOST. Never writes LXC config itself: `tune` and
`create` print `pct set` lines and a reason per line; the operator (or a later --apply on a non-exempt host) runs them.

    runnerprofile.py measure --ct N            append one sample of the CT's cgroup counters to LOG (timer, every min)
    runnerprofile.py tune --ct N [--hours 24]  propose a profile diff from the samples (shadow: prints, changes nothing)
    runnerprofile.py create --ct N [--profile F]  print the pct lines for a profile (default: DEFAULT_PROFILE)
    runnerprofile.py rollback --ct N           print the commands that restore the newest /etc/pve/lxc/N.conf.bak-*

First case: CT103 runner-pve. On 2026-10-28 its jobs thrashed the page cache at the 4 G limit (6.58 M memory.high
events, 460 M file refaults ~ 1.76 TB re-read in 14 d), which drove pve IO pressure to some 80 % / full 47 %.
"""
import argparse
import glob
import json
import math
import os
import re
import sys
import time

CG_ROOT = os.environ.get("RUNNERPROFILE_CG_ROOT", "/sys/fs/cgroup/lxc")
PVE_CONF = os.environ.get("RUNNERPROFILE_PVE_CONF", "/etc/pve/lxc")
LOG_DIR = os.environ.get("RUNNERPROFILE_LOG_DIR", "/var/log/runner-profile")
MEMINFO = os.environ.get("RUNNERPROFILE_MEMINFO", "/proc/meminfo")
HOST_IO_PSI = os.environ.get("RUNNERPROFILE_HOST_IO_PSI", "/proc/pressure/io")
GiB = 1024 ** 3

DEFAULT_PROFILE = {"memory": 8192, "swap": 1024, "cores": 8, "cpulimit": 4, "cpuunits": 10, "io_weight": 10}
# sizing rules (plan §Requirements 3); first guesses, refined by the shadow
REFAULT_GB_DAY_MAX = 20        # file pages re-read per day above this = the working set does not fit
HIGH_EVENTS_DAY_MAX = 1000     # memory.high throttling events per day above this = undersized
SWAP_MAX_MB = 1024             # a runner that swaps is undersized; swap only hides it
HOST_RESERVE_GB = 4            # never propose memory that leaves the host less than this available
THROTTLE_SHARE_MAX = 0.25      # cpu throttled_usec / usage_usec above this with p95 at the limit = cpu-bound
IO_FULL_OK = 5.0               # host io full avg60 p95 below this (and no thrash) = io weight may go back up


def _read(path, default=""):
    try:
        return open(path).read()
    except OSError:
        return default


def _kv(text):
    out = {}
    for line in text.splitlines():
        f = line.split()
        if len(f) == 2 and re.fullmatch(r"-?\d+", f[1]):
            out[f[0]] = int(f[1])
    return out


def _psi(text, kind="some"):
    m = re.search(rf"^{kind}\s+avg10=[\d.]+\s+avg60=([\d.]+)", text, re.M)
    return float(m.group(1)) if m else None


def _io_bytes(text):
    """(rbytes, wbytes) of the busiest device. io.stat lists the device-mapper volumes AND the disk under them
    (252:x on 259:0 for local-lvm), so a sum counts the same IO twice."""
    r = w = 0
    for line in text.splitlines():
        d = dict(p.split("=", 1) for p in line.split()[1:] if "=" in p)
        r = max(r, int(d.get("rbytes", 0)))
        w = max(w, int(d.get("wbytes", 0)))
    return r, w


def pve_conf(ct):
    """The CT's current limits from /etc/pve/lxc/N.conf (memory/swap in MB, as pct uses them)."""
    out = {}
    for line in _read(os.path.join(PVE_CONF, f"{ct}.conf")).splitlines():
        if line.startswith("["):              # snapshot sections follow the live config
            break
        k, _, v = line.partition(":")
        k, v = k.strip(), v.strip()
        if k in ("memory", "swap", "cores", "cpulimit", "cpuunits"):
            out[k] = float(v) if k == "cpulimit" else int(v)
        elif k == "lxc.cgroup2.io.bfq.weight":
            out["io_weight"] = int(v)
    return out


def sample(ct):
    """One snapshot of the CT's cgroup counters (cumulative counters as-is; tune works on deltas)."""
    d = os.path.join(CG_ROOT, str(ct))
    if not os.path.isdir(d):
        raise SystemExit(f"runnerprofile: no cgroup for CT {ct} at {d} (is it running?)")
    ms, ev, cpu = _kv(_read(os.path.join(d, "memory.stat"))), _kv(_read(os.path.join(d, "memory.events"))), \
        _kv(_read(os.path.join(d, "cpu.stat")))
    rb, wb = _io_bytes(_read(os.path.join(d, "io.stat")))

    def num(name):
        v = _read(os.path.join(d, name)).strip()
        return int(v) if v.isdigit() else None
    avail = _kv(_read(MEMINFO).replace(":", "").replace(" kB", "")).get("MemAvailable")
    return {"ts": int(time.time()), "ct": int(ct),
            "mem_current": num("memory.current"), "mem_max": num("memory.max"), "swap_current": num("memory.swap.current"),
            "anon": ms.get("anon"), "file": ms.get("file"), "refault_file": ms.get("workingset_refault_file"),
            "pgmajfault": ms.get("pgmajfault"), "high": ev.get("high"), "max": ev.get("max"), "oom_kill": ev.get("oom_kill"),
            "cpu_usage_usec": cpu.get("usage_usec"), "cpu_throttled_usec": cpu.get("throttled_usec"),
            "io_rbytes": rb, "io_wbytes": wb,
            "mem_psi_some": _psi(_read(os.path.join(d, "memory.pressure"))),
            "io_psi_some": _psi(_read(os.path.join(d, "io.pressure"))),
            "host_io_full": _psi(_read(HOST_IO_PSI), "full"),
            "host_mem_available_kb": avail, "conf": pve_conf(ct)}


def log_path(ct):
    return os.path.join(LOG_DIR, f"ct{ct}.jsonl")


def measure(ct):
    os.makedirs(LOG_DIR, exist_ok=True)
    rec = sample(ct)
    with open(log_path(ct), "a") as fh:
        fh.write(json.dumps(rec) + "\n")
    return rec


def load(ct, hours, path=None):
    since = time.time() - hours * 3600
    rows = []
    try:
        for line in open(path or log_path(ct)):
            try:
                r = json.loads(line)
            except ValueError:
                continue
            if r.get("ts", 0) >= since:
                rows.append(r)
    except OSError:
        pass
    return rows


def _p95(xs):
    xs = sorted(x for x in xs if x is not None)
    return xs[min(len(xs) - 1, int(math.ceil(0.95 * len(xs))) - 1)] if xs else None


def _rate_per_day(rows, key):
    """Counter growth per day over the window; a counter that went DOWN (CT restarted) restarts the sum there."""
    total, prev = 0, None
    for r in rows:
        v = r.get(key)
        if v is None:
            continue
        if prev is not None:
            total += v - prev if v >= prev else v
        prev = v
    span = (rows[-1]["ts"] - rows[0]["ts"]) if len(rows) > 1 else 0
    return total * 86400 / span if span > 0 else None


def tune(rows):
    """{metrics, proposal: {key: (new, reason)}} — never applies. Needs >= 2 samples spanning > 10 min."""
    if len(rows) < 2 or rows[-1]["ts"] - rows[0]["ts"] < 600:
        return {"error": "not enough samples yet (need >= 2 spanning 10 min; run `measure` from the timer)"}
    cur = rows[-1].get("conf") or {}
    m = {"hours": round((rows[-1]["ts"] - rows[0]["ts"]) / 3600, 1), "samples": len(rows),
         "refault_gb_day": None, "high_events_day": _rate_per_day(rows, "high"),
         "oom_kills_day": _rate_per_day(rows, "oom_kill"), "read_gb_day": None,
         "anon_p95_gb": None, "swap_p95_mb": None, "busy_cores_p95": None, "throttle_share": None,
         "host_io_full_p95": _p95([r.get("host_io_full") for r in rows]),
         "host_available_gb": (rows[-1].get("host_mem_available_kb") or 0) / 1024 ** 2}
    rf = _rate_per_day(rows, "refault_file")
    m["refault_gb_day"] = rf * 4096 / GiB if rf is not None else None
    rd = _rate_per_day(rows, "io_rbytes")
    m["read_gb_day"] = rd / GiB if rd is not None else None
    anon = _p95([r.get("anon") for r in rows])
    m["anon_p95_gb"] = anon / GiB if anon is not None else None
    sw = _p95([r.get("swap_current") for r in rows])
    m["swap_p95_mb"] = sw / 2 ** 20 if sw is not None else None
    busy = []
    for a, b in zip(rows, rows[1:]):
        if a.get("cpu_usage_usec") is not None and b.get("cpu_usage_usec") is not None and b["ts"] > a["ts"]:
            busy.append(max(0, b["cpu_usage_usec"] - a["cpu_usage_usec"]) / 1e6 / (b["ts"] - a["ts"]))
    m["busy_cores_p95"] = _p95(busy)
    used = (rows[-1].get("cpu_usage_usec") or 0) - (rows[0].get("cpu_usage_usec") or 0)
    thr = (rows[-1].get("cpu_throttled_usec") or 0) - (rows[0].get("cpu_throttled_usec") or 0)
    m["throttle_share"] = thr / used if used > 0 else None

    prop = {}
    mem = cur.get("memory")
    thrash = (m["refault_gb_day"] or 0) > REFAULT_GB_DAY_MAX or (m["high_events_day"] or 0) > HIGH_EVENTS_DAY_MAX
    if mem and thrash:
        new = mem * 2
        room_mb = (m["host_available_gb"] - HOST_RESERVE_GB) * 1024
        if new - mem > room_mb:
            new = mem + max(0, int(room_mb // 1024) * 1024)
        if new > mem:
            prop["memory"] = (new, f"thrash: {m['refault_gb_day'] or 0:.0f} GB/day file refaults, "
                                   f"{m['high_events_day'] or 0:.0f} memory.high events/day at {mem} MB")
        else:
            prop["memory"] = (mem, f"thrash, but the host has only {m['host_available_gb']:.1f} G available "
                                   f"(reserve {HOST_RESERVE_GB} G): move the runner or free host memory")
    # Never a shrink: a runner's page cache (toolchains, caches, the checkout) is its working set, and sizing from
    # anon memory alone is exactly how CT103 ended up thrashing. anon_p95_gb is reported for the operator only.
    if (cur.get("swap") or 0) > SWAP_MAX_MB:
        prop["swap"] = (SWAP_MAX_MB, f"swap {cur['swap']} MB > {SWAP_MAX_MB}: a runner that swaps is undersized "
                                     f"(p95 in use {m['swap_p95_mb'] or 0:.0f} MB)")
    cl = cur.get("cpulimit")
    if cl and m["busy_cores_p95"] is not None and (m["throttle_share"] or 0) > THROTTLE_SHARE_MAX \
            and m["busy_cores_p95"] >= 0.9 * cl and cl < (cur.get("cores") or cl):
        prop["cpulimit"] = (cl + 1, f"cpu-bound: p95 {m['busy_cores_p95']:.1f} cores at the {cl:g} limit, "
                                    f"{m['throttle_share']:.0%} of cpu time throttled")
    iw = cur.get("io_weight")
    if iw is not None and iw < 50 and not thrash and m["host_io_full_p95"] is not None \
            and m["host_io_full_p95"] < IO_FULL_OK:
        prop["io_weight"] = (50, f"no thrash and host io full p95 {m['host_io_full_p95']:.1f} % < {IO_FULL_OK} %: "
                                 f"the low weight is no longer needed")
    return {"ct": rows[-1]["ct"], "current": cur, "metrics": m,
            "proposal": {k: {"value": v, "reason": r} for k, (v, r) in prop.items() if v != cur.get(k)}}


def pct_lines(ct, values, cur=None):
    """The commands for a profile or a proposal. io weight is a raw lxc key, so it is a conf line, not a pct flag."""
    cur = cur or {}
    flags = [f"--{k} {v:g}" if isinstance(v, float) else f"--{k} {v}"
             for k, v in values.items() if k != "io_weight" and v != cur.get(k)]
    out = [f"cp /etc/pve/lxc/{ct}.conf /etc/pve/lxc/{ct}.conf.bak-$(date +%Y%m%d%H%M)"]
    if flags:
        out.append(f"pct set {ct} " + " ".join(flags))
    if "io_weight" in values and values["io_weight"] != cur.get("io_weight"):
        w = values["io_weight"]
        # first line of the file: snapshot sections live at the end, and this must stay in the live section
        out.append(f"sed -i '/^lxc.cgroup2.io.bfq.weight:/d; 1i lxc.cgroup2.io.bfq.weight: {w}' /etc/pve/lxc/{ct}.conf")
        out.append(f"echo {w} > {CG_ROOT}/{ct}/io.bfq.weight   # the same value live, no restart")
    return out if len(out) > 1 else []


def rollback_lines(ct):
    baks = sorted(glob.glob(os.path.join(PVE_CONF, f"{ct}.conf.bak-*")), key=os.path.getmtime)
    if not baks:
        return None, []
    b = baks[-1]
    conf = pve_conf(ct)
    old = {}
    for line in _read(b).splitlines():
        if line.startswith("["):
            break
        k, _, v = line.partition(":")
        k, v = k.strip(), v.strip()
        if k in ("memory", "swap", "cores", "cpulimit", "cpuunits"):
            old[k] = float(v) if k == "cpulimit" else int(v)
        elif k == "lxc.cgroup2.io.bfq.weight":
            old["io_weight"] = int(v)
    if "io_weight" in conf and "io_weight" not in old:
        old["io_weight"] = 100                 # bfq default when the backup had no override
    return b, pct_lines(ct, old, conf)


def main(argv=None):
    ap = argparse.ArgumentParser(prog="runnerprofile.py", description=__doc__.split("\n")[0])
    ap.add_argument("cmd", choices=["measure", "tune", "create", "rollback"])
    ap.add_argument("--ct", required=True, type=int)
    ap.add_argument("--hours", type=float, default=24)
    ap.add_argument("--profile", default="", help="JSON file with profile keys (memory, swap, cores, cpulimit, "
                                                  "cpuunits, io_weight); default: the built-in DEFAULT_PROFILE")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)
    if a.cmd == "measure":
        rec = measure(a.ct)
        print(json.dumps(rec) if a.json else f"runnerprofile: sampled CT {a.ct} -> {log_path(a.ct)}")
        return 0
    if a.cmd == "tune":
        r = tune(load(a.ct, a.hours))
        if a.json or "error" in r:
            print(json.dumps(r, indent=1))
            return 1 if "error" in r else 0
        m = r["metrics"]
        print(f"CT {a.ct} over {m['hours']} h ({m['samples']} samples): refaults {m['refault_gb_day'] or 0:.1f} GB/day, "
              f"memory.high {m['high_events_day'] or 0:.0f}/day, reads {m['read_gb_day'] or 0:.1f} GB/day, "
              f"busy p95 {m['busy_cores_p95'] or 0:.1f} cores, host io full p95 {m['host_io_full_p95']}")
        if not r["proposal"]:
            print("no change proposed: the current limits fit the measurements")
            return 0
        for k, v in r["proposal"].items():
            print(f"  {k}: {r['current'].get(k)} -> {v['value']}   ({v['reason']})")
        print("commands (operator; nothing was changed):")
        for line in pct_lines(a.ct, {k: v["value"] for k, v in r["proposal"].items()}, r["current"]):
            print("  " + line)
        return 0
    if a.cmd == "create":
        prof = dict(DEFAULT_PROFILE, **(json.load(open(a.profile)) if a.profile else {}))
        for line in pct_lines(a.ct, prof, pve_conf(a.ct)) or ["# CT already matches the profile"]:
            print(line)
        return 0
    b, lines = rollback_lines(a.ct)
    if not b:
        print(f"runnerprofile: no /etc/pve/lxc/{a.ct}.conf.bak-* to roll back to", file=sys.stderr)
        return 1
    print(f"# restore the limits from {b}")
    for line in lines or ["# the current limits already match that backup"]:
        print(line)
    return 0


if __name__ == "__main__":
    sys.exit(main())
