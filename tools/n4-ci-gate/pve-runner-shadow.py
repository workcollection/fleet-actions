#!/usr/bin/env python3
"""runner-pve IO gate SHADOW log (24 h before any switch; orchestrator m-8628, plan pawkit var/plans/n4-ci-gate.md).
Runs on the pve HOST every minute. runner-pve (CT103, smol-kitten) has no start gate today; on 2026-10-28 its jobs
drove pve IO pressure to some 80 % / full 47 %. This logs, per runner service, what a HOLD-ONLY IO gate would do:
    busy runner            -> keep (a running job is never cut)
    idle runner, gate shut -> park  (would stop the idle runner service)
    stopped, gate open     -> start
The gate is gate_policy.decide with the pve config: IO PSI 'full avg60' <= IO_FULL_MAX (20 %), CPU PSI 'some avg60'
< PSI_START, no thermal input, no guaranteed minimum (MIN_RUNNER=0). Changes nothing: no service is touched.

    pve-runner-shadow.py tick                 append one JSON line to LOG (systemd timer, every minute)
    pve-runner-shadow.py summary [--hours 24] per runner: would-park / would-start ticks, maxima of the inputs
"""
import json
import os
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.realpath(__file__)))
import gate_policy as G  # noqa: E402

LOG = os.environ.get("PVE_RUNNER_SHADOW_LOG", "/var/log/pve-runner-shadow.jsonl")
CT = os.environ.get("PVE_RUNNER_CT", "103")
UNIT_GLOB = "actions.runner.*"
PVE_DEFAULTS = {"PSI_FILE": "/proc/pressure/cpu", "TEMP_FILE": "", "IO_PSI_FILE": "/proc/pressure/io",
                "IO_FULL_MAX": "20", "MIN_RUNNER": "0"}
CONF_FILE = os.environ.get("PVE_RUNNER_GATE_CONF", "/etc/pve-runner-gate.conf")


def conf():
    """gate_policy defaults, then the pve ones, then CONF_FILE (KEY=VALUE lines) if present."""
    c = dict(G.DEFAULTS, **PVE_DEFAULTS)
    if os.path.exists(CONF_FILE):
        for line in open(CONF_FILE):
            line = line.split("#", 1)[0].strip()
            if "=" in line:
                k, v = line.split("=", 1)
                c[k.strip()] = v.strip()
    return c


def _ct(*cmd):
    r = subprocess.run(["pct", "exec", CT, "--", *cmd], capture_output=True, text=True, timeout=30)
    return r.stdout if r.returncode == 0 else None


def runners():
    """[{unit, state, dir, busy}] for the runner services in the CT, or None when the CT can't be read."""
    out = _ct("systemctl", "list-units", UNIT_GLOB, "--all", "--plain", "--no-legend", "--type=service")
    if out is None:
        return None
    ps = _ct("ps", "-eo", "args") or ""
    res = []
    for line in out.splitlines():
        f = line.split()
        if not f or not f[0].endswith(".service"):
            continue
        unit, active = f[0], f[2]
        wd = (_ct("systemctl", "show", "-p", "WorkingDirectory", "--value", unit) or "").strip()
        busy = bool(wd) and any("Runner.Worker" in a and a.startswith(wd.rstrip("/") + "/") for a in ps.splitlines())
        res.append({"unit": unit, "state": active, "dir": wd, "busy": busy})
    return res


def plan(rs, allow):
    """The hold-only decision per runner: keep | park | start."""
    out = []
    for r in rs:
        if r["busy"]:
            act = "keep"
        elif r["state"] == "active":
            act = "keep" if allow else "park"
        else:
            act = "start" if allow else "keep"
        out.append(dict(r, new=act))
    return out


def tick():
    c = conf()
    psi, io = G.read_psi(c), G.read_io_full(c)
    rs = runners()
    busy = sum(1 for r in rs or [] if r["busy"])
    allow, why = G.decide(busy, psi, None, c, io=io)
    some = None
    try:
        some = float(open(c["IO_PSI_FILE"]).read().split("avg60=")[1].split()[0])
    except (OSError, IndexError, ValueError):
        pass
    rec = {"ts": int(time.time()), "cpu_some_avg60": psi, "io_some_avg60": some, "io_full_avg60": io,
           "load1": float(open("/proc/loadavg").read().split()[0]), "allow": allow, "why": why,
           "runners": plan(rs, allow) if rs is not None else None}
    with open(LOG, "a") as fh:
        fh.write(json.dumps(rec) + "\n")
    return rec


def summary(hours=24, path=None):
    since = time.time() - hours * 3600
    rows = []
    for line in open(path or LOG):
        try:
            r = json.loads(line)
        except ValueError:
            continue
        if r.get("ts", 0) >= since:
            rows.append(r)

    def mx(k):
        return max((r[k] for r in rows if r.get(k) is not None), default=None)
    out = {"ticks": len(rows), "hours": hours, "gate_shut": sum(1 for r in rows if not r["allow"]),
           "ct_unreadable": sum(1 for r in rows if r.get("runners") is None),
           "max_io_full_avg60": mx("io_full_avg60"), "max_io_some_avg60": mx("io_some_avg60"),
           "max_cpu_some_avg60": mx("cpu_some_avg60"), "max_load1": mx("load1"), "runners": {}}
    for r in rows:
        for x in r.get("runners") or []:
            u = out["runners"].setdefault(x["unit"], {"busy": 0, "would_park": 0, "would_start": 0,
                                                      "held_busy_while_shut": 0})
            u["busy"] += x["busy"]
            u["would_park"] += x["new"] == "park"
            u["would_start"] += x["new"] == "start"
            u["held_busy_while_shut"] += x["busy"] and not r["allow"]
    return out


def main(argv=None):
    a = argv if argv is not None else sys.argv[1:]
    if a[:1] == ["tick"]:
        tick()
        return 0
    if a[:1] == ["summary"]:
        h = float(a[a.index("--hours") + 1]) if "--hours" in a else 24
        print(json.dumps(summary(h), indent=1))
        return 0
    print(__doc__, file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
