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
     ALERT_MIN, or when the agent cannot be reached;
  6. writes one JSON line per tick (each host's capacity, the queue, the
     placement a multi-host router would choose, what it started, the API
     budget) and a heartbeat file (runner router phase 1a).

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
    # For allowlisted PUBLIC repos, serve only these events, triggered by OWNER
    # from the repo itself: never pull_request / pull_request_target.
    "PUBLIC_EVENTS": "push,workflow_dispatch",
    "EXCLUDE_WORKFLOWS": "",  # comma list of owner/repo:path or owner/repo:path#job name
    "JOB_LABELS_OK": "self-hosted,linux,x64,polo-nyan",
    # A dedicated pool (e.g. signing) runs its own minter instance with
    # PN_MINTER_CONF=<its conf> and sets these:
    #   ONLY_REPOS     serve only these repos (comma list; empty = all).
    #   REQUIRE_LABELS a job is eligible only if it carries ALL of these labels.
    # With REQUIRE_LABELS set, a repo is skipped (and paged) while it has ANY queued
    # job whose labels fit JOB_LABELS_OK but lack REQUIRE_LABELS: GitHub could hand
    # that job to this pool's runner (a user account has no runner groups).
    "ONLY_REPOS": "",
    "REQUIRE_LABELS": "",
    "ALERT_MIN": "30",
    # Page only on a true stall: over ALERT_MIN, the gate was open (free slot AND host
    # load1 < LOAD_MAX) for at least STALL_SHARE of the ticks. A queue that only waits on
    # the load gate or the cap is logged, not paged.
    "LOAD_MAX": "8",
    "STALL_SHARE": "0.5",
    "ALERT_REPEAT_MIN": "60",
    "REPO_CACHE_S": "3600",
    "SWEEP_S": "300",
    "STATE": "/var/lib/pn-minter/state.json",
    "NOTIFY_CMD": "",
    # Runner router phase 1a (smol-kitten/repo.catboy.systems docs/plans/runner-router-phase1.md §3).
    # HOSTS_FILE: one line per host, `name=n4 cmd="ssh … pn-agent" weight=1.0 drain=0`. Empty or
    # absent file = one host, AGENT_CMD, as before. Opt-in per instance, so a second pool (signing)
    # never reads the general pool's hosts. Runners start on the FIRST line only; further lines get
    # their status polled and a shadow placement logged.
    "HOSTS_FILE": "",
    # Per-tick JSON log and heartbeat. Empty = next to STATE (state.json -> state.ticks.jsonl,
    # state.heartbeat.json), so two instances never share a file. "-" = off.
    "TICK_LOG": "",
    "TICK_LOG_MAX_MB": "10",
    "HEARTBEAT": "",
    # Owners whose queue this token cannot see; logged as "not visible", never as an empty queue.
    "NOT_VISIBLE": "",
    # Own count of billable (non-304) API calls in the last hour; at this many the tick skips
    # GitHub until older calls age out. 0 = no own cap (the X-RateLimit-Remaining backoff stays on).
    "API_BUDGET_H": "600",
    # Below 1500 / 500 remaining (shared token): run a full tick at most every SLOW_TICK_S;
    # below 500 also no sweep and no repo-list refresh; below 200 pause until the reset.
    "SLOW_TICK_S": "300",
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
        # API budget: times of billable calls (a 304 on a conditional request is free), and the
        # shared token's X-RateLimit-Remaining/Reset as of the last response.
        self.api = state.setdefault("api", {"calls": [], "remaining": None, "reset": 0})

    def _note(self, headers, billable):
        if billable:
            self.api["calls"].append(int(time.time()))
        if headers is not None and headers.get("X-RateLimit-Remaining") is not None:
            self.api["remaining"] = int(headers["X-RateLimit-Remaining"])
            self.api["reset"] = int(headers.get("X-RateLimit-Reset") or 0)

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
                self._note(r.headers, True)
                body = r.read()
                data = json.loads(body) if body else None
                if cache and r.headers.get("ETag"):
                    self.etags[path] = {"etag": r.headers["ETag"], "data": data}
                return data
        except urllib.error.HTTPError as e:
            self._note(e.headers, e.code != 304)
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


def agent(c, sub, stdin=None, cmd=None):
    cmd = shlex.split(cmd or c["AGENT_CMD"]) + [sub]
    return subprocess.run(cmd, input=stdin, capture_output=True, text=True, timeout=120)


def hosts(c):
    """The host registry. No HOSTS_FILE (or no such file): one host, AGENT_CMD, exactly as before.
    A file that exists but does not parse raises: it must not fall back silently."""
    path = c["HOSTS_FILE"]
    if not path or not os.path.exists(path):
        return [{"name": "default", "cmd": c["AGENT_CMD"], "weight": 1.0, "drain": False}]
    out = []
    for n, line in enumerate(open(path), 1):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        kv = dict(x.split("=", 1) for x in shlex.split(line) if "=" in x)
        if not kv.get("name") or not kv.get("cmd"):
            raise ValueError(f"{path}:{n}: a host line needs name= and cmd=")
        if kv["name"] in {h["name"] for h in out}:
            raise ValueError(f"{path}:{n}: host {kv['name']} is listed twice")
        out.append({"name": kv["name"], "cmd": kv["cmd"], "weight": float(kv.get("weight", "1")),
                    "drain": kv.get("drain", "0") == "1"})
    if not out:
        raise ValueError(f"{path}: no host lines")
    return out


def host_view(h, status):
    """One host's line in the tick log, from its `pn-agent status` (None = unreachable)."""
    if status is None:
        return {"name": h["name"], "state": "unreachable", "slots_free": 0}
    cap = status.get("capacity")
    running = sum(1 for x in status.get("containers", []) if x.get("state") == "running")
    slots = 0 if h["drain"] or not isinstance(cap, dict) else int(cap.get("slots_free", 0))
    return {"name": h["name"], "state": "drain" if h["drain"] else "ok", "running": running,
            "max_jobs": status.get("max_jobs"), "slots_free": slots, "capacity": cap,
            "gate": status.get("gate")}


def place(hs, views, jobs):
    """Shadow placement (logged, not acted on): each job, oldest first, goes to the host with a free
    slot and the highest weight; ties go to the fewest running, then registry order."""
    free = {v["name"]: v["slots_free"] for v in views}
    running = {v["name"]: v.get("running", 0) for v in views}
    out = []
    for j in jobs:
        cand = [h for h in hs if free[h["name"]] > 0]
        best = max(cand, key=lambda h: (h["weight"], -running[h["name"]]), default=None)
        if best:
            free[best["name"]] -= 1
            running[best["name"]] += 1
        out.append({"repo": j["repo"], "job": j["job"], "host": best["name"] if best else None})
    return out


def budget_step(c, api, now):
    """ok | slow | lean | pause | over (runner-router.md §6a). Prunes the call list to the last hour."""
    api["calls"] = [t for t in api.get("calls", []) if now - t < 3600]
    if int(c["API_BUDGET_H"]) and len(api["calls"]) >= int(c["API_BUDGET_H"]):
        return "over"
    rem = api.get("remaining")
    if rem is None or now >= api.get("reset", 0):
        return "ok"  # unknown, or the rate-limit window has reset since the last response
    if rem < 200:
        return "pause"
    if rem < 500:
        return "lean"
    if rem < 1500:
        return "slow"
    return "ok"


def side_path(c, key, suffix):
    v = c[key]
    if v == "-":
        return ""
    return v or os.path.splitext(c["STATE"])[0] + suffix


def write_tick(c, rec):
    """Append the tick line (size-capped: one rotation to .1) and replace the heartbeat. A write
    error is logged, never raised: the log must not stop runners from starting."""
    path = side_path(c, "TICK_LOG", ".ticks.jsonl")
    try:
        if path:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            if os.path.exists(path) and os.path.getsize(path) > float(c["TICK_LOG_MAX_MB"]) * 1e6:
                os.replace(path, path + ".1")
            with open(path, "a") as f:
                f.write(json.dumps(rec, separators=(",", ":")) + "\n")
        hb = side_path(c, "HEARTBEAT", ".heartbeat.json")
        if hb:
            os.makedirs(os.path.dirname(hb), exist_ok=True)
            with open(hb + ".tmp", "w") as f:
                json.dump({"at": rec["at"], "agent_ok": rec.get("agent_ok"), "budget": rec.get("budget"),
                           "eligible": rec.get("queued", {}).get("eligible")}, f)
            os.replace(hb + ".tmp", hb)
    except OSError as e:
        log(f"WARN tick log/heartbeat not written: {e}")


def repos(c, gh, state, refresh=True):
    """Non-archived repos of OWNER, split into eligible and excluded-public."""
    now = time.time()
    cache = state.get("repos")
    if not cache or (refresh and now - cache["at"] > int(c["REPO_CACHE_S"])):
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


def served_repos(c, repos_ok):
    """Apply the ONLY_REPOS allowlist (empty = every repo in repos_ok)."""
    only = {x.strip() for x in c["ONLY_REPOS"].split(",") if x.strip()}
    return [r for r in repos_ok if r in only] if only else list(repos_ok)


def queued_jobs(c, gh, repo, public=False):
    """Queued jobs of the repo, each with an 'eligible' verdict and reason."""
    labels_ok = {x.strip().lower() for x in c["JOB_LABELS_OK"].split(",")}
    required = {x.strip().lower() for x in c["REQUIRE_LABELS"].split(",") if x.strip()}
    public_events = {x.strip() for x in c["PUBLIC_EVENTS"].split(",") if x.strip()}
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
                if "self-hosted" not in labels and not (required and labels & required):
                    continue  # GitHub-hosted job, not ours
                if required and labels <= labels_ok and not required <= labels:
                    reason = "UNSAFE: fits this pool's runner labels without " + ",".join(sorted(required))
                elif required and not required <= labels:
                    continue  # not this pool's job
                elif not labels <= labels_ok:
                    reason = "labels " + ",".join(sorted(labels - labels_ok))
                elif f"{repo}:{run['path']}" in excluded or \
                        f"{repo}:{run['path']}#{j['name']}" in excluded:
                    reason = "excluded (needs docker)"
                elif run["event"] == "pull_request" and \
                        (run.get("head_repository") or {}).get("full_name") != repo:
                    reason = "pull_request from a fork"
                elif public and (run["event"] not in public_events
                                 or (run.get("actor") or {}).get("login") != c["OWNER"]
                                 or (run.get("head_repository") or {}).get("full_name") != repo):
                    reason = f"public repo: only {','.join(sorted(public_events))} by {c['OWNER']}"
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
    try:
        hs = hosts(c)
    except (OSError, ValueError) as e:
        notify(c, state, "hosts", f"pn-minter: host registry unusable, no runner started: {e}")
        save_state(c["STATE"], state)
        return 1
    c["AGENT_CMD"] = hs[0]["cmd"]  # phase 1a: runners start on the first host only
    now = time.time()
    tick = mode == ""  # only real ticks are budget-gated and write the tick log + heartbeat
    rec = {"at": int(now), "hosts": [], "agent_ok": False,
           "not_visible": {o.strip(): "not visible" for o in c["NOT_VISIBLE"].split(",") if o.strip()}}

    def finish(rc):
        if tick:
            gh.api["calls"] = [t for t in gh.api["calls"] if time.time() - t < 3600]
            rec["api"] = {"billable_1h": len(gh.api["calls"]), "remaining": gh.api.get("remaining")}
            write_tick(c, rec)
        save_state(c["STATE"], state)
        return rc

    st = agent(c, "status")
    if st.returncode != 0:
        notify(c, state, "agent-down", f"pn-minter: pn-agent status failed: {st.stderr.strip()[:200]}")
        rec["hosts"] = [host_view(hs[0], None)]
        return finish(1)
    status = json.loads(st.stdout)
    rec["agent_ok"] = True
    views = [host_view(hs[0], status)]
    for h in hs[1:]:
        try:
            r = agent(c, "status", cmd=h["cmd"])
            views.append(host_view(h, json.loads(r.stdout) if r.returncode == 0 else None))
        except (subprocess.TimeoutExpired, OSError, ValueError):
            views.append(host_view(h, None))
    rec["hosts"] = views

    # API budget (runner-router.md §6a). The step is logged when it changes and paged once per
    # ALERT_REPEAT_MIN while it is not ok. --check and --dry-run are never held back.
    step = budget_step(c, gh.api, now)
    rec["budget"] = step
    if tick and step != state.get("budget_step", "ok"):
        log(f"API budget step {state.get('budget_step', 'ok')} -> {step}: {len(gh.api['calls'])} billable "
            f"calls in the last hour, {gh.api.get('remaining')} remaining on the token")
        state["budget_step"] = step
    if tick and step != "ok":
        notify(c, state, "budget", f"pn-minter: GitHub API budget step '{step}': {len(gh.api['calls'])} billable "
               f"calls in the last hour (cap {c['API_BUDGET_H']}), {gh.api.get('remaining')} remaining on the "
               f"shared token. Runner starts are slowed or paused until it recovers.")
    if tick and (step in ("over", "pause") or
                 (step in ("slow", "lean") and now - state.get("full_tick", 0) < int(c["SLOW_TICK_S"]))):
        rec["skipped"] = step
        return finish(0)
    if tick:
        state["full_tick"] = now

    live = [x for x in status["containers"] if x["state"] == "running"]
    idle = {}
    for x in live:
        if not x["job"]:
            idle[x["repo"]] = idle.get(x["repo"], 0) + 1

    ok, public = repos(c, gh, state, refresh=step != "lean")
    allow = {x.strip() for x in c["ALLOW_PUBLIC"].split(",") if x.strip()}
    ok = served_repos(c, ok)
    jobs = []
    for repo in ok:
        jobs += queued_jobs(c, gh, repo, public=repo in allow)
    # A repo with an UNSAFE job gets no runner from this pool until that job is gone.
    unsafe = {j["repo"] for j in jobs if (j["reason"] or "").startswith("UNSAFE")}
    for repo in sorted(unsafe):
        notify(c, state, f"unsafe:{repo}", f"pn-minter: {repo} has a queued job that could land on the "
               f"{c['REQUIRE_LABELS']} runner without carrying that label; not starting one there.")
    jobs = [dict(j, eligible=False, reason=j["reason"] or "repo has an UNSAFE job")
            if j["repo"] in unsafe else j for j in jobs]

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
        for v in views:
            print(f"host {v['name']}: {v['state']}, slots_free {v['slots_free']}")
        return finish(0)

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
    # Gate-open history over the alert window: (time, slot free and load below LOAD_MAX).
    now = time.time()
    # the agent reports its own gate decision (psi or load, n4-ci-gate) since 2026-10-28; older agents: the load rule
    gate = status.get("gate") or {}
    gate_open = len(live) < status["max_jobs"] and (gate["allow"] if "allow" in gate
                                                    else status["load1"] < float(c["LOAD_MAX"]))
    hist = [h for h in state.get("gate_hist", []) if now - h[0] <= int(c["ALERT_MIN"]) * 60]
    hist.append([now, gate_open])
    state["gate_hist"] = hist
    open_share = sum(1 for _, o in hist if o) / len(hist)
    if stale:
        worst = sorted(stale.items(), key=lambda kv: -kv[1][1])
        text = (f"pn-minter: {sum(n for n, _ in stale.values())} polo-nyan job(s) queued over "
                f"{c['ALERT_MIN']} min in {len(stale)} repo(s); oldest "
                + ", ".join(f"{r.split('/', 1)[1]} {o:.0f} min" for r, (n, o) in worst[:5])
                + f". Runners {len(live)}/{status['max_jobs']}, load {status['load1']}, "
                f"gate open {open_share:.0%} of the last {c['ALERT_MIN']} min.")
        if gate_open and open_share >= float(c["STALL_SHARE"]):
            notify(c, state, "queue", text + " STALL: runners idle with the gate open.")
        else:
            log("queue waits on the load gate or the cap (not paged): " + text)

    rec["queued"] = {"eligible": len(eligible), "need": len(need), "skipped": len(jobs) - len(eligible)}
    rec["placement"] = place(hs, views, need)
    rec["started"] = []
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
            rec["started"].append({"repo": j["repo"], "job": j["job"], "host": hs[0]["name"]})
            log(f"started runner for {j['repo']} ({j['name']}): {r.stdout.strip().splitlines()[-1]}")
        else:
            log(f"agent refused start for {j['repo']} (exit {r.returncode}): {r.stdout.strip()[-200:]}")
            break  # cap, load or disk: the next tick retries

    # Sweep only on ticks that started nothing, against a FRESH container list:
    # a runner that registered seconds ago is briefly "offline" on GitHub, and
    # the status taken at the start of this tick does not know it yet.
    if mode != "--dry-run" and not started and step != "lean" and \
            time.time() - state.get("swept", 0) > int(c["SWEEP_S"]):
        fresh = agent(c, "status")
        if fresh.returncode == 0:
            sweep(c, gh, state, {x["name"] for x in json.loads(fresh.stdout)["containers"]})
            state["swept"] = time.time()
    return finish(0)


if __name__ == "__main__":
    sys.exit(main())
