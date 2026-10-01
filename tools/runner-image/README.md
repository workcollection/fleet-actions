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

## Native (non-docker) runners: dpkg-divert

Runners that run natively on a host (systemd units, for example `runner-pve-r1`/`-r2`) have no image to rebuild. Their `.path` is usually `/sbin:/bin:/usr/sbin:/usr/bin`, which has no `/usr/local/bin`. So the 2.14 binary goes to **`/usr/bin/osslsigncode`**, behind a diversion:

```sh
dpkg-divert --local --rename --divert /usr/bin/osslsigncode.distrib --add /usr/bin/osslsigncode
install -m 0755 osslsigncode-2.14 /usr/bin/osslsigncode   # e.g. copied out of the image: docker cp $(docker create <image>):/usr/local/bin/osslsigncode .
```

- **What the diversion does:** apt still owns its package, but upgrades write to `.distrib`, so an `apt upgrade` can no longer silently replace 2.14 with the distro's 2.8.
- **Check:** `dpkg-divert --list | grep osslsigncode` shows it, and `PATH=$(cat <runner>/.path) osslsigncode --version` shows what jobs see.
- **Undo:** `rm /usr/bin/osslsigncode && dpkg-divert --rename --remove /usr/bin/osslsigncode`.
- **Binary compatibility:** the binary from this image is dynamically linked for Ubuntu noble. It runs on noble hosts; check `ldd` elsewhere.
