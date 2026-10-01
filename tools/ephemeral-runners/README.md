# Ephemeral repository runners (pn-minter + pn-agent)

Two small scripts give a GitHub **user** account (which has no organization runner groups) one fresh, single-use runner container per queued job. Each container registers to one repository with `--ephemeral`, takes one job and is removed.

| Part | Runs on | Holds | Does |
|---|---|---|---|
| `pn-minter.py` | the box that holds the GitHub token | the account token (`GH_TOKEN`) | finds queued jobs, mints 1-hour repository registration tokens, sends each one to the agent on stdin, deletes stale registrations, pages when the queue is old |
| `pn-agent.py` | the docker host | no GitHub credential | checks the caps, starts one container per request, enforces the job timeout, the disk budget and the idle limit |

The account token never reaches the docker host. The docker host only sees registration tokens. Each of those is valid for one repository for one hour.

## Guardrails

| Guardrail | Where | Default |
|---|---|---|
| One container per job, `--rm`, `--ephemeral`, no restart policy | agent | always |
| Concurrent containers | agent `MAX_JOBS` | 3 |
| CPU, memory (no extra swap), PIDs per container | agent `CPUS`, `MEMORY`, `PIDS` | 2, 3g, 2048 |
| Writable-layer budget per container (killed above it) | agent `DISK_MAX_GB` | 12 |
| Minimum free disk to start | agent `DISK_FREE_MIN_GB` | 30 |
| Job timeout (stopped after it) | agent `JOB_TIMEOUT_MIN` | 60 |
| Idle container with no job (stopped after it) | agent `IDLE_MAX_MIN` | 15 |
| Skip starts while host 1-minute load is above | agent `LOAD_MAX` | 8 |
| No `docker.sock`, no account token in job containers | agent | always |
| Public repositories are not served unless listed | minter `ALLOW_PUBLIC` | empty |
| Listed public repositories: only these events, run by the owner from the repo itself | minter `PUBLIC_EVENTS` | push,workflow_dispatch |
| `pull_request` runs from forks are not served | minter | always |
| Jobs that need a docker daemon are skipped | minter `EXCLUDE_WORKFLOWS` | per site |
| Page only on a true stall: jobs over `ALERT_MIN` while the gate (free slot and load below `LOAD_MAX`) was open for `STALL_SHARE` of that time | minter | 30 min, 8, 0.5 |

## How the registration token travels

1. The minter calls `POST /repos/{owner}/{repo}/actions/runners/registration-token`.
2. It writes `REPO=...` and `RUNNER_TOKEN=...` to the agent's stdin (for example through `ssh ... pn-agent start`). The token is never on a command line.
3. The agent writes the token to a `0600` file in a `0700` directory and bind-mounts it read-only into the container.
4. A wrapper entrypoint reads the file into `RUNNER_TOKEN` and starts the image's own entrypoint with `UNSET_CONFIG_VARS=true`. So the token is not in the container config (`docker inspect`) and not in the job's environment.
5. `pn-agent reap` deletes the file when the runner reports "Listening for Jobs", or after `TOKEN_FILE_MAX_S` (120 s).

## Stale registrations

A runner that is stopped before it takes a job does not deregister itself. That includes idle-limit stops and failed starts. Every `SWEEP_S`, the minter deletes `pn-*` registrations that are offline and that no container backs. It sweeps only on ticks that started no runner, and it reads a fresh container list first. A runner that registered seconds ago shows "offline" on GitHub for a moment, so a stale list would delete a live registration.

This design has no persistent `.runner` file, so it cannot fall into the "already configured" restart loop that long-lived containers hit after an ungraceful kill. Every container starts from a clean image and is removed with `--rm`.

## Install

On the docker host:

```sh
install -m 0755 pn-agent.py /usr/local/sbin/pn-agent
install -d /etc/pn-runner && cp agent.conf.example /etc/pn-runner/agent.conf   # then edit it
cp systemd/pn-agent-reap.service systemd/pn-agent-reap.timer /etc/systemd/system/
systemctl daemon-reload && systemctl enable --now pn-agent-reap.timer
```

On the token box:

```sh
install -m 0755 pn-minter.py /usr/local/sbin/pn-minter
cp minter.conf.example /etc/pn-runner/minter.conf   # set AGENT_CMD and the exclusions
cp systemd/pn-minter.service systemd/pn-minter.timer /etc/systemd/system/   # set how GH_TOKEN is injected
systemctl daemon-reload && systemctl enable --now pn-minter.timer
```

Pin the runner image by digest in `agent.conf`.

## Operate

```sh
GH_TOKEN=... pn-minter --check     # queued jobs (eligible or skipped, with the reason) vs running containers
GH_TOKEN=... pn-minter --dry-run   # one tick: print what would start; mint, start and page nothing
pn-agent status                    # JSON: load, free disk, containers (age, job or idle, writable-layer size)
journalctl -t pn-runner/<name>     # one container's runner log (the agent uses the journald log driver)
```

## Limits you accept

- **Jobs that need docker fail on these runners.** Examples: `docker build`, `docker compose`, `services:`, `container:`, buildx actions. List them in `EXCLUDE_WORKFLOWS` so they stay queued and visible instead of failing. Move them to a separate builder (see below).
- **The docker host is the unit of compromise.** If other containers on the same host mount `docker.sock`, any job on those containers can reach these containers and the short-lived token files. Put the agent on a host without such neighbours when you can.

## Jobs that need docker: options

| Option | Isolation | Cost |
|---|---|---|
| GitHub-hosted runners (`runs-on: ubuntu-latest`) for those jobs only | best | Actions minutes on private repositories |
| A separate builder host (its own docker daemon, the agent there with `docker.sock` mounted, no other secrets on it) | good: a job that escapes finds nothing of value | one more small host |
| In-container builders (buildah or kaniko) | medium | workflow changes; does not cover `services:` |

## Dedicated signing pool

The same two scripts run a second, single-slot pool that takes **only** signing jobs, on a host of its own. A second minter instance (`PN_MINTER_CONF=/etc/pn-runner/signing.conf`) feeds a pn-agent on the signing host. See `signing/` for the example configs; the image is `tools/runner-image/Dockerfile` (built as `pn-sign-runner:1`).

### Trust boundary

| What | Where | Why |
|---|---|---|
| GitHub account token | token box only | The signing host sees only 1-hour, one-repo registration tokens, the same as the general pool. |
| Signing identity (PKI AppRole `role_id` + `secret_id`, issuer, URLs) | signing host `/etc/catboy/runner*`, mounted read-only into the job container | Only the job container on this host can use it. The PKI side binds the `secret_id` to this host's address, and issuance also needs the GitHub OIDC claims of the pinned signing workflow, so a stolen `secret_id` alone cannot sign. |
| docker.sock, other host mounts, other runners | none | A job that escapes its container finds only the signing identity. Nothing else lives on this host. |

### Which jobs can reach the signing runner

A user account has no runner groups. GitHub gives a queued job to any runner of that repository whose labels cover the job's `runs-on`. So the signing runner carries **exactly one unique label**, for example `your-user-signing`:

1. **Repository allowlist:** `ONLY_REPOS`. Signing runners register only to those repositories.
2. **One label, no defaults:** `LABELS=your-user-signing` with `NO_DEFAULT_LABELS=1`. The runner has no `self-hosted`, `Linux` or `X64` label. Only a job whose `runs-on` is exactly that label can land on it. A bare `runs-on: self-hosted` or `[self-hosted, linux]` job cannot.
3. **Callers:** use `runs-on: your-user-signing` verbatim. `catboy-sign.yml` passes `runs-on` through as a string, and a list does not work there.
4. **Required label + guard (belt and braces):** `REQUIRE_LABELS=your-user-signing`. The minter starts a signing runner only for those jobs. If an allowlisted repository has a queued job that fits the runner's labels without carrying the required one, no runner is started there and the minter pages.

`test_minter_labels.py` covers the label routing for both pools.
