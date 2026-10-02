# mari-gate: cap and load-gate a long-lived runner pool

Use this when a fixed pool of self-hosted runner containers shares a host with workloads that matter more, such as game servers. A long-lived runner takes a job whenever it is idle and online, so the gate controls **which runners are online**:

| Condition | Action |
|---|---|
| host 1-minute load ≥ `LOAD_MAX` (8) | park every **idle** runner; busy runners finish their job |
| more than `CAP` (6) runners online | park idle runners, highest index first |
| fewer than `CAP` online, and `HOLD_S` (120 s) since the last park | unpark runners, lowest index first, with your launcher |

**Another runner pool on the same host** (for example ephemeral runners): set `OTHER_JOB_PREFIX` to
its container name prefix. Each such running container counts as one job against `CAP`, so `CAP`
means "jobs on this host" and this pool gets `CAP` minus their number (never below 0). The gate
only counts those containers; it never stops them. Unset (the default) keeps the old behaviour.

- **Park** marks the index in `/var/lib/mari-gate/parked/`, runs `docker stop -t 30` (the runner entrypoint deregisters on SIGTERM) and removes the container.
- **Unpark** runs `LAUNCH_CMD <index>`, which re-registers the runner, then removes the mark.
- `/var/lib/mari-gate/status` holds the last decision's numbers. The journal (`journalctl -t mari-gate`) holds every park and unpark.

## Accepted race

GitHub can assign a job to an idle runner in the second between the gate's idle check and the stop. That job fails and needs a rerun. A long-lived runner gives no atomic "stop taking jobs" switch, so this is accepted.

## Install

```sh
install -m 0755 mari-gate.sh /usr/local/sbin/mari-gate
cp mari-gate.conf.example /etc/mari-gate.conf        # set RUNNER_PREFIX, LAUNCH_CMD; start with MODE=observe
cp mari-gate.service mari-gate.timer /etc/systemd/system/
systemctl daemon-reload && systemctl enable --now mari-gate.timer
journalctl -t mari-gate -f                            # watch what it would do
sed -i 's/^MODE=.*/MODE=enforce/' /etc/mari-gate.conf # then enforce
```

## Two changes outside this directory

1. **Launcher: CPU limit per runner.** The cap limits jobs, not cores, and one build job can use many cores. Add a CPU limit to the `docker run` line of your launcher:

   ```sh
   docker run -d ... --cpus "${RUNNER_CPUS:-2}" ... <image>
   ```

   The launcher must accept a single index (`launcher 3` recreates only runner 3), because the gate unparks one runner at a time.

2. **Relauncher: skip parked runners.** Any job that relaunches "missing" runners (a disk guard, a self-heal cron) would undo the cap. Skip the parked indices in its loop:

   ```sh
   for i in $(seq 1 10); do
     [ -e /var/lib/mari-gate/parked/$i ] && continue   # parked by mari-gate
     docker ps --format '{{.Names}}' | grep -qx "runner-$i" || missing="$missing $i"
   done
   ```

   A full relaunch of every runner (`launcher` with no arguments) also overrides the gate. Run it only with the gate's timer stopped.

## Measure the effect

Log the host load and CPU package temperature once a minute, for example from `/sys/class/hwmon/*/temp*_input` (label `Package id 0` on Intel, `Tctl` on AMD). Compare a day before enabling with a day after.
