# runner-profile

Sizes a dedicated CI runner LXC from what its jobs measurably use, on the Proxmox host. Orchestrator m-8646 (4); the plan is in pawkit `var/plans/runner-lxc-profile.md`.

**It never changes LXC config.** `tune`, `create` and `rollback` print `pct set` / conf lines with a reason for each, and the operator runs them. pve is on the exempt list.

| command | what it does |
|---|---|
| `measure --ct N` | Appends one sample of the CT's cgroup counters to `/var/log/runner-profile/ctN.jsonl`. Run every minute by `runnerprofile-measure@N.timer`. |
| `tune --ct N [--hours 24]` | Proposes a diff from the samples, with the measured reason per line. Prints only. |
| `create --ct N [--profile F]` | Prints the lines for a profile (default: memory 8192, swap 1024, cores 8, cpulimit 4, cpuunits 10, io weight 10). |
| `rollback --ct N` | Prints the lines that restore the limits from the newest `/etc/pve/lxc/N.conf.bak-*`. Every printed change starts with such a backup. |

## Sizing rules (first guess; refine from the shadow)

- **memory:** doubled while the CT thrashes, meaning file refaults above 20 GB/day or more than 1000 memory.high events/day. The raise is capped at the host's available memory minus a 4 G reserve. It is **never shrunk**: a runner's page cache is its working set, and sizing from anon memory alone is how CT103 ended up thrashing.
- **swap:** at most 1 G. A runner that swaps is undersized.
- **cpulimit:** +1, up to `cores`, when p95 busy cores reach the limit and more than 25 % of CPU time is throttled.
- **io weight** (`lxc.cgroup2.io.bfq.weight`): back up to 50 once there is no thrash and host IO full p95 is below 5 %.

## Install on pve (operator)

```
install -D -m 755 runnerprofile.py /usr/local/lib/runner-profile/runnerprofile.py
cp systemd/runnerprofile-measure@.* /etc/systemd/system/ && systemctl daemon-reload
systemctl enable --now runnerprofile-measure@103.timer
# after 24 h:
python3 /usr/local/lib/runner-profile/runnerprofile.py tune --ct 103
```

Test: `python3 tools/runner-profile/test_runnerprofile.py` (a fake cgroup and pve tree; no host access).
