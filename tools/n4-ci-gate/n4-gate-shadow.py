#!/usr/bin/env python3
"""n4 CI gate SHADOW log (24 h before the switch; plan pawkit var/plans/n4-ci-gate.md, operator OK 2026-10-27).
Every minute, for each pool on CT113, logs the decision the CURRENT gate takes and the one the NEW shared rule
(gate_policy.decide) would take. Changes nothing: no runner is started or parked by this script.

    n4-gate-shadow.py tick                 append one JSON line to LOG (systemd timer, every minute)
    n4-gate-shadow.py summary [--hours 24] would-start vs refused counts per pool and rule, max PSI / temperature
"""
import json
import os
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.realpath(__file__)))
import gate_policy as G  # noqa: E402

LOG = os.environ.get("N4_GATE_SHADOW_LOG", "/var/log/n4-gate-shadow.jsonl")
PN_CONF = "/etc/pn-runner/agent.conf"
MARI_CONF = "/etc/mari-gate.conf"
MARI_STATUS = "/var/lib/mari-gate/status"
MARI_LOAD_MAX_DEFAULT = 8          # mari-gate.sh's default when its conf does not set LOAD_MAX


def _kv(path):
    out = {}
    try:
        for line in open(path):
            line = line.split("#", 1)[0].strip()
            if "=" in line:
                k, v = line.split("=", 1)
                out[k.strip()] = v.strip()
    except OSError:
        pass
    return out


def pn_running():
    r = subprocess.run(["docker", "ps", "--filter", "label=pn.managed=1", "-q"], capture_output=True, text=True,
                       timeout=30)
    return len([x for x in r.stdout.split() if x]) if r.returncode == 0 else None


def mari_status():
    """{running, busy, mode, ladder} from mari-gate's status line, or {}."""
    try:
        line = open(MARI_STATUS).read().split("\n")[0]
    except OSError:
        return {}
    return dict(p.split("=", 1) for p in line.split()[1:] if "=" in p)


def tick():
    c = G.conf()
    psi, temp = G.read_psi(c), G.read_temp(c)
    load1 = float(open("/proc/loadavg").read().split()[0])
    pn = _kv(PN_CONF)
    pn_n = pn_running()
    pn_max, pn_load = int(pn.get("MAX_JOBS", 3)), float(pn.get("LOAD_MAX", 8))
    ms = mari_status()
    mari_busy = int(ms.get("busy", 0) or 0)
    mari_load = float(_kv(MARI_CONF).get("LOAD_MAX", MARI_LOAD_MAX_DEFAULT))
    rec = {"ts": int(time.time()), "load1": load1, "psi_avg60": psi, "temp_c": temp,
           "pn": {"running": pn_n, "current": bool(pn_n is not None and pn_n < pn_max and load1 <= pn_load),
                  "current_why": f"load1 {load1:.1f} {'<=' if load1 <= pn_load else '>'} {pn_load:g}"},
           "mari": {"running": int(ms.get("running", 0) or 0), "busy": mari_busy, "mode": ms.get("mode"),
                    "ladder": ms.get("ladder"), "current": load1 < mari_load,
                    "current_why": f"load1 {load1:.1f} vs LOAD_MAX {mari_load:g} (unpark gate)"}}
    for pool, n in (("pn", pn_n or 0), ("mari", mari_busy)):
        allow, why = G.decide(n, psi, temp, c)
        rec[pool]["new"], rec[pool]["new_why"] = allow, why
        if pool == "pn" and pn_n is not None and pn_n >= pn_max:
            rec[pool]["new"], rec[pool]["new_why"] = False, f"cap reached ({pn_n}/{pn_max})"
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
    out = {"ticks": len(rows), "hours": hours,
           "max_psi_avg60": max((r["psi_avg60"] for r in rows if r.get("psi_avg60") is not None), default=None),
           "max_temp_c": max((r["temp_c"] for r in rows if r.get("temp_c") is not None), default=None),
           "max_load1": max((r["load1"] for r in rows), default=None)}
    for pool in ("pn", "mari"):
        cur = sum(1 for r in rows if r[pool]["current"])
        new = sum(1 for r in rows if r[pool]["new"])
        out[pool] = {"current_allow": cur, "current_refuse": len(rows) - cur, "new_allow": new,
                     "new_refuse": len(rows) - new,
                     "differ": sum(1 for r in rows if r[pool]["current"] != r[pool]["new"]),
                     "new_refuse_thermal": sum(1 for r in rows if "thermal" in r[pool]["new_why"])}
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
