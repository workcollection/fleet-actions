#!/usr/bin/env python3
"""n4 CI gate policy: ONE rule for starting a CI runner on n4, shared by pn-agent (polo-nyan) and mari-gate
(the mari pool). Operator OK 2026-10-27 ('N4 gate ok'), plan pawkit var/plans/n4-ci-gate.md.

A new runner may start when
    the host package temperature is below TEMP_STOP (default 85 C; thermal is the hard stop), AND
    host CPU pressure 'some avg60' is below PSI_START (default 40 %), OR
    this pool runs no runner at all and the pressure is below PSI_MIN_RUNNER (default 70 %).
So each pool always gets one runner unless thermal is critical or the box is truly saturated.

Inputs come from files the n4 host writes into CT113 every ~15 s (mari-gate-hostpsi): a missing or stale
(older than INPUT_MAX_AGE_S) input refuses the start, with the reason.

    gate_policy.py decide --pool-running N    prints {"allow": bool, "reason": "...", "psi": .., "temp": ..}
"""
import json
import os
import re
import sys
import time

DEFAULTS = {
    "PSI_FILE": "/run/mari-gate/host-cpu-pressure",
    "TEMP_FILE": "/run/mari-gate/host-pkg-temp",
    "INPUT_MAX_AGE_S": "120",
    "PSI_START": "40",
    "PSI_MIN_RUNNER": "70",
    "TEMP_STOP": "85",
    # optional terms (pve, 2026-10-28): IO_FULL_MAX = refuse when IO PSI 'full avg60' exceeds it (empty = off);
    # TEMP_FILE empty = no thermal input on that host; MIN_RUNNER=0 = no guaranteed minimum
    "IO_PSI_FILE": "/proc/pressure/io",
    "IO_FULL_MAX": "",
    "MIN_RUNNER": "1",
}
CONF_FILE = os.environ.get("N4_GATE_CONF", "/etc/n4-ci-gate.conf")


def conf(path=None):
    c = dict(DEFAULTS)
    p = path or CONF_FILE
    if os.path.exists(p):
        for line in open(p):
            line = line.split("#", 1)[0].strip()
            if "=" in line:
                k, v = line.split("=", 1)
                c[k.strip()] = v.strip()
    return c


def _fresh(path, max_age):
    if path.startswith("/proc/"):      # live kernel file: its mtime is boot time, never stale
        return os.path.exists(path)
    try:
        return time.time() - os.path.getmtime(path) <= max_age
    except OSError:
        return False


def read_psi(c):
    """Host CPU pressure 'some avg60' (%), or None when missing/stale."""
    if not _fresh(c["PSI_FILE"], float(c["INPUT_MAX_AGE_S"])):
        return None
    m = re.search(r"some\s+avg10=[\d.]+\s+avg60=([\d.]+)", open(c["PSI_FILE"]).read())
    return float(m.group(1)) if m else None


def read_temp(c):
    """Host package temperature (C), or None when missing/stale. The file holds millidegrees (hwmon)."""
    if not _fresh(c["TEMP_FILE"], float(c["INPUT_MAX_AGE_S"])):
        return None
    try:
        v = float(open(c["TEMP_FILE"]).read().split()[0])
    except (ValueError, IndexError, OSError):
        return None
    return v / 1000 if v > 1000 else v


def read_io_full(c):
    """IO PSI 'full avg60' (%) from IO_PSI_FILE (live /proc: no staleness check), or None."""
    try:
        m = re.search(r"^full\s+avg10=[\d.]+\s+avg60=([\d.]+)", open(c["IO_PSI_FILE"]).read(), re.M)
    except OSError:
        return None
    return float(m.group(1)) if m else None


def decide(pool_running, psi, temp, c=None, io=None):
    """(allow, reason). Pure: the inputs are passed in."""
    c = dict(DEFAULTS, **(c or {}))
    if c.get("TEMP_FILE"):
        if temp is None:
            return False, "temperature input missing or stale"
        if temp >= float(c["TEMP_STOP"]):
            return False, f"thermal stop: package {temp:.0f} C >= {c['TEMP_STOP']} C"
    if c.get("IO_FULL_MAX"):
        if io is None:
            return False, "io pressure input missing"
        if io > float(c["IO_FULL_MAX"]):
            return False, f"io pressure full {io:.1f} % > {c['IO_FULL_MAX']} %"
    if psi is None:
        return False, "cpu pressure input missing or stale"
    if psi < float(c["PSI_START"]):
        return True, f"cpu pressure {psi:.1f} % < {c['PSI_START']} %"
    if pool_running == 0 and c.get("MIN_RUNNER", "1") != "0" and psi < float(c["PSI_MIN_RUNNER"]):
        return True, f"guaranteed minimum: pool idle, cpu pressure {psi:.1f} % < {c['PSI_MIN_RUNNER']} %"
    return False, (f"cpu pressure {psi:.1f} % >= {c['PSI_START']} % and the pool already runs {pool_running}"
                   if pool_running else f"cpu pressure {psi:.1f} % >= {c['PSI_MIN_RUNNER']} % (box saturated)")


def main(argv=None):
    a = argv if argv is not None else sys.argv[1:]
    if a[:1] != ["decide"] or "--pool-running" not in a:
        print(__doc__.strip().splitlines()[-1].strip(), file=sys.stderr)
        return 2
    n = int(a[a.index("--pool-running") + 1])
    c = conf()
    psi, temp = read_psi(c), read_temp(c)
    allow, why = decide(n, psi, temp, c)
    print(json.dumps({"allow": allow, "reason": why, "psi": psi, "temp": temp, "pool_running": n}))
    return 0 if allow else 1


if __name__ == "__main__":
    sys.exit(main())
