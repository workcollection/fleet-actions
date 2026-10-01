#!/usr/bin/env python3
"""pn-agent: the docker-host side of the ephemeral runner pair.

It runs on the host that runs the job containers. It holds NO GitHub
credential. The minter (pn-minter.py) sends it one registration token per
start request on stdin.

Commands:
  start   Read KEY=VALUE lines on stdin (REPO, NAME, RUNNER_TOKEN). Check the
          caps, then start one ephemeral runner container. Exit codes:
          0 started, 10 concurrency cap, 11 load too high, 12 disk too low,
          2 bad input, 1 docker failure.
  status  Print a JSON list of the managed containers.
  reap    Enforce the job timeout, the disk budget and the idle limit. Run it
          from a timer.

The registration token never goes on argv or into the container config. It is
written to a 0600 file, bind-mounted read-only, read by a small wrapper
entrypoint, and deleted as soon as the runner has registered (or after
TOKEN_FILE_MAX_S).
"""
import json
import os
import re
import secrets
import shlex
import shutil
import subprocess
import sys
import time

CONF_FILE = os.environ.get("PN_AGENT_CONF", "/etc/pn-runner/agent.conf")
DEFAULTS = {
    "IMAGE": "myoung34/github-runner:ubuntu-noble",
    "LABELS": "self-hosted,linux,polo-nyan",
    "OWNER": "polo-nyan",
    "MAX_JOBS": "3",
    "CPUS": "2",
    "MEMORY": "3g",
    "PIDS": "2048",
    "DISK_MAX_GB": "12",
    "DISK_FREE_MIN_GB": "30",
    "JOB_TIMEOUT_MIN": "60",
    "IDLE_MAX_MIN": "15",
    "LOAD_MAX": "8",
    "TOKEN_DIR": "/run/pn-runner",
    "TOKEN_FILE_MAX_S": "120",
    # Extra `docker run` arguments, shell-quoted, e.g. a signing identity:
    # --env-file /etc/catboy/runner.env -v /etc/catboy/runner:/run/catboy:ro
    "EXTRA_RUN_ARGS": "",
    # 1 = register with only LABELS (no self-hosted/Linux/X64 defaults), so a job
    # that asks for e.g. [self-hosted, linux] can never land on this runner.
    "NO_DEFAULT_LABELS": "",
}
PREFIX = "pn-"
LABEL = "pn.managed"
NAME_RE = re.compile(r"^pn-[a-z0-9-]{3,58}$")

# Wrapper entrypoint: read the token from the mounted file, then hand over to
# the image's own entrypoint. The file itself is deleted on the host side.
WRAPPER = (
    'RUNNER_TOKEN="$(cat /run/pn-token/token)"; export RUNNER_TOKEN; '
    'exec /entrypoint.sh ./bin/Runner.Listener run --startuptype service'
)


def conf():
    c = dict(DEFAULTS)
    if os.path.exists(CONF_FILE):
        for line in open(CONF_FILE):
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                c[k.strip()] = v.strip()
    return c


def log(msg):
    print(f"pn-agent: {msg}", flush=True)


def docker(*args, check=True, capture=True):
    r = subprocess.run(["docker", *args], capture_output=capture, text=True)
    if check and r.returncode != 0:
        raise RuntimeError(f"docker {args[0]} failed: {r.stderr.strip()[:300]}")
    return r


def managed():
    """Return the managed containers, running or not.

    `docker ps --size` fails when a container's files change while docker
    measures them (seen: apt replacing a file mid-job). Sizes are then left
    at 0 for this pass instead of failing the whole listing.
    Even the plain listing can fail for a moment while a --rm container is
    being removed ("rw layer snapshot not found"), so retry it.
    """
    args = ["ps", "-a", "--filter", f"label={LABEL}=1", "--format", "{{json .}}"]
    r = docker(*args[:2], "--size", *args[2:], check=False)
    for attempt in range(3):
        if r.returncode == 0:
            break
        time.sleep(1)
        r = docker(*args, check=False)
    if r.returncode != 0:
        raise RuntimeError(f"docker ps failed 3 times: {r.stderr.strip()[:300]}")
    out = r.stdout
    rows = []
    for line in out.splitlines():
        d = json.loads(line)
        labels = dict(kv.split("=", 1) for kv in d.get("Labels", "").split(",") if "=" in kv)
        rows.append({
            "name": d["Names"],
            "state": d["State"],
            "repo": labels.get("pn.repo", ""),
            "started": int(labels.get("pn.started", "0") or 0),
            "size_rw": parse_size(d.get("Size", "0B")),
        })
    return rows


def parse_size(s):
    """'474MB (virtual 1.14GB)' -> bytes of the writable layer."""
    m = re.match(r"\s*([\d.]+)\s*([kKMGT]?B)", s)
    if not m:
        return 0
    mult = {"B": 1, "kB": 1e3, "KB": 1e3, "MB": 1e6, "GB": 1e9, "TB": 1e12}[m.group(2)]
    return int(float(m.group(1)) * mult)


def running_job(name):
    """True once the runner has picked up a job."""
    r = docker("logs", "--tail", "400", name, check=False)
    return "Running job:" in (r.stdout + r.stderr)


def registered(name):
    r = docker("logs", "--tail", "200", name, check=False)
    out = r.stdout + r.stderr
    return "Listening for Jobs" in out or "Running job:" in out


def load1():
    return float(open("/proc/loadavg").read().split()[0])


def disk_free_gb(path="/var/lib/docker"):
    p = path if os.path.exists(path) else "/"
    return shutil.disk_usage(p).free / 1e9


def cmd_start(c):
    fields = {}
    for line in sys.stdin.read().splitlines():
        if "=" in line:
            k, v = line.split("=", 1)
            fields[k.strip()] = v.strip()
    repo, token = fields.get("REPO", ""), fields.get("RUNNER_TOKEN", "")
    owner = c["OWNER"]
    if not re.fullmatch(rf"{re.escape(owner)}/[A-Za-z0-9._-]+", repo) or not token:
        log("start: bad input (REPO or RUNNER_TOKEN missing or invalid)")
        return 2
    live = [r for r in managed() if r["state"] in ("running", "created", "restarting")]
    if len(live) >= int(c["MAX_JOBS"]):
        log(f"start {repo}: cap reached ({len(live)}/{c['MAX_JOBS']})")
        return 10
    if load1() > float(c["LOAD_MAX"]):
        log(f"start {repo}: load {load1():.2f} > {c['LOAD_MAX']}, skipping")
        return 11
    if disk_free_gb() < float(c["DISK_FREE_MIN_GB"]):
        log(f"start {repo}: disk free {disk_free_gb():.0f} GB < {c['DISK_FREE_MIN_GB']} GB")
        return 12
    slug = re.sub(r"[^a-z0-9-]+", "-", repo.split("/", 1)[1].lower()).strip("-")[:40]
    name = f"{PREFIX}{slug}-{secrets.token_hex(3)}"
    if not NAME_RE.match(name):
        log(f"start: generated name {name!r} is invalid")
        return 2
    tdir = os.path.join(c["TOKEN_DIR"], name)
    os.makedirs(tdir, mode=0o700, exist_ok=True)
    tfile = os.path.join(tdir, "token")
    fd = os.open(tfile, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(token)
    now = int(time.time())
    args = [
        "run", "-d", "--rm", "--name", name,
        "--label", f"{LABEL}=1", "--label", f"pn.repo={repo}", "--label", f"pn.started={now}",
        "--cpus", c["CPUS"], "--memory", c["MEMORY"], "--memory-swap", c["MEMORY"],
        "--pids-limit", c["PIDS"],
        "--log-driver", "journald", "--log-opt", f"tag=pn-runner/{name}",
        "-v", f"{tdir}:/run/pn-token:ro",
        "-e", f"REPO_URL=https://github.com/{repo}", "-e", "RUNNER_SCOPE=repo",
        "-e", f"RUNNER_NAME={name}", "-e", f"LABELS={c['LABELS']}",
        "-e", "EPHEMERAL=1", "-e", "DISABLE_AUTO_UPDATE=1", "-e", "UNSET_CONFIG_VARS=true",
        "-e", "RUNNER_WORKDIR=/tmp/runner/work",
        *(["-e", "NO_DEFAULT_LABELS=1"] if c["NO_DEFAULT_LABELS"] == "1" else []),
        *shlex.split(c["EXTRA_RUN_ARGS"]),
        "--entrypoint", "/bin/bash", c["IMAGE"], "-c", WRAPPER,
    ]
    r = docker(*args, check=False)
    if r.returncode != 0:
        shutil.rmtree(tdir, ignore_errors=True)
        log(f"start {repo}: docker run failed: {r.stderr.strip()[:300]}")
        return 1
    log(f"started {name} for {repo}")
    print(json.dumps({"name": name, "repo": repo}))
    return 0


def remove_token_dir(c, name):
    shutil.rmtree(os.path.join(c["TOKEN_DIR"], name), ignore_errors=True)


def cmd_reap(c):
    now = time.time()
    timeout = int(c["JOB_TIMEOUT_MIN"]) * 60
    idle_max = int(c["IDLE_MAX_MIN"]) * 60
    disk_max = float(c["DISK_MAX_GB"]) * 1e9
    token_max = int(c["TOKEN_FILE_MAX_S"])
    rows = managed()
    names = {r["name"] for r in rows}
    for r in rows:
        name, age = r["name"], now - r["started"]
        if r["state"] != "running":
            if r["state"] in ("exited", "dead", "created") and age > 300:
                docker("rm", "-f", name, check=False)
                remove_token_dir(c, name)
            continue
        # The token file is only needed until the runner has registered.
        tdir = os.path.join(c["TOKEN_DIR"], name)
        if os.path.isdir(tdir) and (registered(name) or age > token_max):
            remove_token_dir(c, name)
        if age > timeout:
            log(f"reap {name} ({r['repo']}): job timeout {age / 60:.0f} min > {timeout // 60} min")
            docker("stop", "-t", "30", name, check=False)
        elif r["size_rw"] > disk_max:
            log(f"reap {name} ({r['repo']}): writable layer {r['size_rw'] / 1e9:.1f} GB > {disk_max / 1e9:.0f} GB")
            docker("kill", name, check=False)
        elif age > idle_max and not running_job(name):
            log(f"reap {name} ({r['repo']}): idle {age / 60:.0f} min with no job")
            docker("stop", "-t", "30", name, check=False)
    # Token dirs whose container is gone (docker run failed, or --rm removed it).
    if os.path.isdir(c["TOKEN_DIR"]):
        for d in os.listdir(c["TOKEN_DIR"]):
            if d not in names:
                remove_token_dir(c, d)
    return 0


def cmd_status(c):
    now = time.time()
    rows = managed()
    for r in rows:
        r["age_min"] = round((now - r["started"]) / 60, 1)
        r["job"] = r["state"] == "running" and running_job(r["name"])
    print(json.dumps({"load1": load1(), "disk_free_gb": round(disk_free_gb(), 1),
                      "max_jobs": int(c["MAX_JOBS"]), "containers": rows}))
    return 0


def main():
    if len(sys.argv) != 2 or sys.argv[1] not in ("start", "status", "reap"):
        print(__doc__, file=sys.stderr)
        return 2
    c = conf()
    os.makedirs(c["TOKEN_DIR"], mode=0o700, exist_ok=True)
    return {"start": cmd_start, "status": cmd_status, "reap": cmd_reap}[sys.argv[1]](c)


if __name__ == "__main__":
    sys.exit(main())
