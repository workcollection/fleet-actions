#!/usr/bin/env python3
"""pn-minter: the credential side of the ephemeral runner pair.

It runs where the GitHub token lives (env GH_TOKEN). On every tick it:
  1. asks the docker host (pn-agent status) what is running;
  2. finds queued self-hosted jobs in the owner's repositories (ETag
     conditional requests, so unchanged lists cost no rate limit);
  3. for each job without an idle runner, mints a 1-hour repository
     registration token and sends it to `pn-agent start` on stdin;
  4. every SWEEP_S, deletes offline pn-* registrations that no container
     backs (a runner killed before it took a job never deregisters itself);
  5. pages through notify when an eligible job has been queued longer than
     ALERT_MIN, or when the agent cannot be reached.

The GitHub token never leaves this process. The docker host only ever sees
single-repository registration tokens.

Usage:
  pn-minter.py            one tick
  pn-minter.py --dry-run  one tick, print decisions, mint and start nothing
  pn-minter.py --check    print queued jobs vs running containers, then exit
"""
import calendar
import json
import os
import shlex
import subprocess
import sys
import time
import urllib.error
import urllib.request

CONF_FILE = os.environ.get("PN_MINTER_CONF", "/etc/pn-runner/minter.conf")
DEFAULTS = {
    "OWNER": "polo-nyan",
    "AGENT_CMD": "pn-agent",
    "ALLOW_PUBLIC": "",
    "EXCLUDE_WORKFLOWS": "",  # comma list of owner/repo:path or owner/repo:path#job name
    "JOB_LABELS_OK": "self-hosted,linux,x64,polo-nyan",
    "ALERT_MIN": "30",
    "ALERT_REPEAT_MIN": "60",
    "REPO_CACHE_S": "3600",
    "SWEEP_S": "300",
    "STATE": "/var/lib/pn-minter/state.json",
    "NOTIFY_CMD": "",
}
API = "https://api.github.com"


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
    print(f"pn-minter: {msg}", flush=True)


class GitHub:
    def __init__(self, token, state):
        self.token = token
        self.etags = state.setdefault("etags", {})

    def call(self, method, path, cache=False):
        req = urllib.request.Request(API + path, method=method)
        req.add_header("Authorization", f"Bearer {self.token}")
        req.add_header("Accept", "application/vnd.github+json")
        req.add_header("X-GitHub-Api-Version", "2022-11-28")
        cached = self.etags.get(path) if cache else None
        if cached:
            req.add_header("If-None-Match", cached["etag"])
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                body = r.read()
                data = json.loads(body) if body else None
                if cache and r.headers.get("ETag"):
                    self.etags[path] = {"etag": r.headers["ETag"], "data": data}
                return data
        except urllib.error.HTTPError as e:
            if e.code == 304 and cached:
                return cached["data"]
            raise


def load_state(path):
    try:
        return json.load(open(path))
    except (OSError, ValueError):
        return {}


def save_state(path, state):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(state, f)
    os.replace(tmp, path)


def agent(c, sub, stdin=None):
    cmd = shlex.split(c["AGENT_CMD"]) + [sub]
    return subprocess.run(cmd, input=stdin, capture_output=True, text=True, timeout=120)


def repos(c, gh, state):
    """Non-archived repos of OWNER, split into eligible and excluded-public."""
    now = time.time()
    cache = state.get("repos")
    if not cache or now - cache["at"] > int(c["REPO_CACHE_S"]):
        rows, page = [], 1
        while True:
            batch = gh.call("GET", f"/user/repos?affiliation=owner&per_page=100&page={page}")
            rows += [r for r in batch if r["owner"]["login"] == c["OWNER"]]
            if len(batch) < 100:
                break
            page += 1
        cache = {"at": now, "list": [{"name": r["full_name"], "private": r["private"],
                                       "archived": r["archived"]} for r in rows]}
        state["repos"] = cache
    allow = {x.strip() for x in c["ALLOW_PUBLIC"].split(",") if x.strip()}
    ok, public = [], []
    for r in cache["list"]:
        if r["archived"]:
            continue
        (ok if r["private"] or r["name"] in allow else public).append(r["name"])
    return ok, public


def queued_jobs(c, gh, repo):
    """Queued jobs of the repo, each with an 'eligible' verdict and reason."""
    labels_ok = {x.strip().lower() for x in c["JOB_LABELS_OK"].split(",")}
    excluded = {x.strip() for x in c["EXCLUDE_WORKFLOWS"].split(",") if x.strip()}
    out = []
    for status in ("queued", "in_progress"):
        runs = gh.call("GET", f"/repos/{repo}/actions/runs?status={status}&per_page=30", cache=True)
        for run in (runs or {}).get("workflow_runs", []):
            jobs = gh.call("GET", f"/repos/{repo}/actions/runs/{run['id']}/jobs?filter=latest&per_page=100", cache=True)
            for j in jobs.get("jobs", []):
                if j["status"] != "queued":
                    continue
                labels = {x.lower() for x in j.get("labels", [])}
                reason = None
                if "self-hosted" not in labels:
                    continue  # GitHub-hosted job, not ours
                if not labels <= labels_ok:
                    reason = "labels " + ",".join(sorted(labels - labels_ok))
                elif f"{repo}:{run['path']}" in excluded or \
                        f"{repo}:{run['path']}#{j['name']}" in excluded:
                    reason = "excluded (needs docker)"
                elif run["event"] == "pull_request" and \
                        (run.get("head_repository") or {}).get("full_name") != repo:
                    reason = "pull_request from a fork"
                out.append({"repo": repo, "run": run["id"], "job": j["id"], "name": j["name"],
                            "workflow": run["path"], "created": j["created_at"],
                            "eligible": reason is None, "reason": reason})
    return out


def age_min(iso):
    t = calendar.timegm(time.strptime(iso, "%Y-%m-%dT%H:%M:%SZ"))
    return (time.time() - t) / 60


def notify(c, state, key, text):
    last = state.setdefault("alerts", {}).get(key, 0)
    if time.time() - last < int(c["ALERT_REPEAT_MIN"]) * 60:
        return
    state["alerts"][key] = time.time()
    log(f"ALERT {text}")
    if c["NOTIFY_CMD"] and not c.get("_dry_run"):
        subprocess.run(shlex.split(c["NOTIFY_CMD"]) + [text], capture_output=True, timeout=60)


def sweep(c, gh, state, live_names):
    now = time.time()
    touched = {r: t for r, t in state.get("touched", {}).items() if now - t < 86400}
    state["touched"] = touched
    for repo in touched:
        runners = gh.call("GET", f"/repos/{repo}/actions/runners?per_page=100")
        for r in runners.get("runners", []):
            if r["name"].startswith("pn-") and r["status"] == "offline" and r["name"] not in live_names:
                gh.call("DELETE", f"/repos/{repo}/actions/runners/{r['id']}")
                log(f"sweep: deleted stale registration {r['name']} on {repo}")


def main():
    mode = sys.argv[1] if len(sys.argv) > 1 else ""
    if mode not in ("", "--dry-run", "--check"):
        print(__doc__, file=sys.stderr)
        return 2
    c = conf()
    c["_dry_run"] = mode == "--dry-run"
    token = os.environ.get("GH_TOKEN", "")
    if not token:
        log("GH_TOKEN is not set")
        return 2
    state = load_state(c["STATE"])
    gh = GitHub(token, state)

    st = agent(c, "status")
    if st.returncode != 0:
        notify(c, state, "agent-down", f"pn-minter: pn-agent status failed: {st.stderr.strip()[:200]}")
        save_state(c["STATE"], state)
        return 1
    status = json.loads(st.stdout)
    live = [x for x in status["containers"] if x["state"] == "running"]
    idle = {}
    for x in live:
        if not x["job"]:
            idle[x["repo"]] = idle.get(x["repo"], 0) + 1

    ok, public = repos(c, gh, state)
    jobs = []
    for repo in ok:
        jobs += queued_jobs(c, gh, repo)

    if mode == "--check":
        print(f"host load1={status['load1']} disk_free={status['disk_free_gb']} GB "
              f"containers={len(live)}/{status['max_jobs']}")
        for x in live:
            print(f"  container {x['name']} repo={x['repo']} age={x['age_min']} min "
                  f"{'RUNNING JOB' if x['job'] else 'idle'} rw={x['size_rw'] / 1e9:.2f} GB")
        print(f"queued self-hosted jobs: {len(jobs)} "
              f"({sum(j['eligible'] for j in jobs)} eligible)")
        for j in sorted(jobs, key=lambda j: j["created"]):
            verdict = "eligible" if j["eligible"] else f"skipped: {j['reason']}"
            print(f"  {j['repo']} {j['workflow']} / {j['name']} "
                  f"queued {age_min(j['created']):.0f} min  {verdict}")
        print(f"public repos not served (not in ALLOW_PUBLIC): {len(public)}")
        save_state(c["STATE"], state)
        return 0

    eligible = sorted((j for j in jobs if j["eligible"]), key=lambda j: j["created"])
    need, stale = [], {}
    spare = dict(idle)
    for j in eligible:
        if spare.get(j["repo"], 0) > 0:
            spare[j["repo"]] -= 1
        else:
            need.append(j)
        if age_min(j["created"]) > int(c["ALERT_MIN"]):
            n, oldest = stale.get(j["repo"], (0, 0))
            stale[j["repo"]] = (n + 1, max(oldest, age_min(j["created"])))
    if stale:
        worst = sorted(stale.items(), key=lambda kv: -kv[1][1])
        notify(c, state, "queue",
               f"pn-minter: {sum(n for n, _ in stale.values())} polo-nyan job(s) queued over "
               f"{c['ALERT_MIN']} min in {len(stale)} repo(s); oldest "
               + ", ".join(f"{r.split('/', 1)[1]} {o:.0f} min" for r, (n, o) in worst[:5])
               + f". Runners {len(live)}/{status['max_jobs']}, load {status['load1']}.")

    slots = status["max_jobs"] - len(live)
    started = 0
    for j in need[:max(slots, 0)]:
        if mode == "--dry-run":
            log(f"dry-run: would start a runner for {j['repo']} ({j['name']})")
            continue
        reg = gh.call("POST", f"/repos/{j['repo']}/actions/runners/registration-token")
        r = agent(c, "start", stdin=f"REPO={j['repo']}\nRUNNER_TOKEN={reg['token']}\n")
        if r.returncode == 0:
            state.setdefault("touched", {})[j["repo"]] = time.time()
            started += 1
            log(f"started runner for {j['repo']} ({j['name']}): {r.stdout.strip().splitlines()[-1]}")
        else:
            log(f"agent refused start for {j['repo']} (exit {r.returncode}): {r.stdout.strip()[-200:]}")
            break  # cap, load or disk: the next tick retries

    # Sweep only on ticks that started nothing, against a FRESH container list:
    # a runner that registered seconds ago is briefly "offline" on GitHub, and
    # the status taken at the start of this tick does not know it yet.
    if mode != "--dry-run" and not started and \
            time.time() - state.get("swept", 0) > int(c["SWEEP_S"]):
        fresh = agent(c, "status")
        if fresh.returncode == 0:
            sweep(c, gh, state, {x["name"] for x in json.loads(fresh.stdout)["containers"]})
            state["swept"] = time.time()
    save_state(c["STATE"], state)
    return 0


if __name__ == "__main__":
    sys.exit(main())
