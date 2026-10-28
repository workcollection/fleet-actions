# n4 CI gate (one start rule for both runner pools on n4 CT113)

Operator OK 2026-10-27 ("N4 gate ok"); plan: pawkit `var/plans/n4-ci-gate.md` (orchestrator m-8413/m-8421).

**Why:** pn-agent (polo-nyan) refused every start at `load1 > 8` while the mari pool kept n4 at load 12-46 all day:
polo-nyan p90 queue wait 233 min on 2026-10-27. Load counts threads queued under the runners' CPU caps, not CPU
headroom (load 26 with ~7 of 12 cores busy, CPU PSI 20-39 %).

**Rule** (`gate_policy.py`, shared): start when the package temperature is below 85 C (hard stop) AND
(host CPU PSI some avg60 < 40 %, OR the pool runs no runner and PSI < 70 %). Inputs come from files the host writes
into CT113 (`host/mari-gate-hostpsi.sh`, every ~15 s); missing or stale inputs refuse. MAX_JOBS 3, CPUS 2,
MEMORY 3g and the disk guardrails are unchanged.

| file | where | what |
|---|---|---|
| `gate_policy.py` | CT113 `/usr/local/lib/n4-ci-gate/` | the rule; `decide --pool-running N` |
| `n4-gate-shadow.py` + `systemd/n4-gate-shadow.*` | CT113 | shadow log: current vs new decision per pool, every minute, `/var/log/n4-gate-shadow.jsonl`; `summary --hours 24` |
| `host/mari-gate-hostpsi.sh` | n4 host `/usr/local/sbin/` | exports host CPU PSI, wing PSI and now the package temperature |
| `test_gate_policy.py` | | tests |

## Rollout
1. Shadow, 24 h: install the files, enable `n4-gate-shadow.timer`. Changes nothing.
2. pn-agent: calls `gate_policy.decide` instead of the `LOAD_MAX` check (follow-up PR). Rollback: revert that one line.
3. mari-gate: same rule for its unpark/start check, hold-only and emergency park kept (follow-up PR). Rollback likewise.

## Install (shadow)
```
# CT113
install -d /usr/local/lib/n4-ci-gate
install -m 0755 gate_policy.py n4-gate-shadow.py /usr/local/lib/n4-ci-gate/
cp systemd/n4-gate-shadow.service systemd/n4-gate-shadow.timer /etc/systemd/system/
systemctl daemon-reload && systemctl enable --now n4-gate-shadow.timer
# n4 host
install -m 0755 host/mari-gate-hostpsi.sh /usr/local/sbin/mari-gate-hostpsi.sh
```

## runner-pve IO gate shadow (pve host, 2026-10-28, orchestrator m-8628)

runner-pve (CT103, smol-kitten) has no start gate. On 2026-10-28 its jobs read ~383 MB/s and pushed pve IO pressure to
some 80 % / full 47 %. `pve-runner-shadow.py` logs, every minute on the pve **host**, what a **hold-only** IO gate
would do per runner service: busy runner = keep (a job is never cut), idle runner with the gate shut = park, stopped
runner with the gate open = start. It changes nothing.

The gate is `gate_policy.decide` with the optional terms added for pve (all off by default, so n4 is unchanged):
`IO_FULL_MAX=20` (IO PSI full avg60), `TEMP_FILE=` (no thermal input), `MIN_RUNNER=0` (no guaranteed minimum),
`PSI_FILE=/proc/pressure/cpu`. Overrides go in `/etc/pve-runner-gate.conf` (KEY=VALUE).

Install on pve (operator): copy `gate_policy.py` and `pve-runner-shadow.py` to `/usr/local/lib/n4-ci-gate/`, the two
`systemd/pve-runner-shadow.*` units to `/etc/systemd/system/`, then `systemctl enable --now pve-runner-shadow.timer`.
After 24 h: `python3 /usr/local/lib/n4-ci-gate/pve-runner-shadow.py summary --hours 24`.
