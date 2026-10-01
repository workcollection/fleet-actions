# Fleet runner image

`Dockerfile` = the digest-pinned `myoung34/github-runner` base plus **osslsigncode 2.14**, built from the sha256-pinned tag archive in a builder stage. The final stage asserts the exact version. The image carries **no signing identity**: a signing runner mounts that read-only at run time.

| Pool | Build | Run |
|---|---|---|
| Long-lived org pool (`run-runners.sh`) | `docker build -t fleet-runner:noble-oss214 tools/runner-image` | set the launcher's image to `fleet-runner:noble-oss214`, then recreate the runners |
| Ephemeral signing pool (pn-agent) | `docker build -t pn-sign-runner:1 tools/runner-image` | `IMAGE=pn-sign-runner:1` in `agent.conf` |

## Why

`catboy-sign` needs osslsigncode >= 2.13, the release with the memory-safety fixes. Ubuntu noble ships 2.8. From catboy-sign 2.1.7, a runner without >= 2.13 does not sign (gate 3). The workflow has no apt fallback.

## Switching a long-lived pool

Recreating every runner aborts the jobs running on them. So:
1. Choose a calm slot.
2. Stop any gate that recreates runners, for example `mari-gate.timer`.
3. Recreate the runners.
4. Check `osslsigncode --version` inside each runner.
5. Restart the gate.
