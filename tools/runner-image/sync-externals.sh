#!/bin/bash
# Copy the runner image's /actions-runner/externals to the same path on the docker host.
# Usage: sync-externals.sh <image> [host-dir, default /actions-runner/externals]
#
# Runners that use the host's docker.sock start `container:` jobs on the HOST daemon with
# -v /actions-runner/externals:/__e. The host resolves that path itself; when it does not exist,
# docker mounts an empty dir and every node action in the job fails with
# 'stat /__e/node24/bin/node: no such file' (n4 CT113, t-1b0787). Run this after the image is
# built or ensured. It copies through a temp dir beside the target, swaps only when the content
# differs, keeps the node20 -> node24 links, prints what it did and exits non-zero on any failure
# (the old copy is then left as it was). No runner restart is needed.
set -euo pipefail
img="${1:?usage: sync-externals.sh <image> [host-dir]}"
dest="${2:-/actions-runner/externals}"
dest="${dest%/}"
parent="$(dirname "$dest")"
mkdir -p "$parent"
tmp="$(mktemp -d "$dest.new.XXXXXX")"
cid=""
cleanup() { [ -n "$cid" ] && docker rm -f "$cid" >/dev/null 2>&1; rm -rf "$tmp" "$dest.old"; }
trap cleanup EXIT
cid="$(docker create "$img")" || { echo "sync-externals: FAILED: docker create $img" >&2; exit 1; }
docker cp "$cid:/actions-runner/externals/." "$tmp/" \
  || { echo "sync-externals: FAILED: copying /actions-runner/externals out of $img" >&2; exit 1; }
[ -x "$tmp/node24/bin/node" ] \
  || { echo "sync-externals: FAILED: $img has no node24/bin/node in /actions-runner/externals; $dest unchanged" >&2; exit 1; }
chmod 755 "$tmp"   # mktemp makes it 700; the job container reads it as /__e
if [ -d "$dest" ] && diff -r --no-dereference -q "$tmp" "$dest" >/dev/null 2>&1; then
  echo "sync-externals: $dest already matches $img; nothing changed"
  exit 0
fi
verb=created
if [ -e "$dest" ]; then mv "$dest" "$dest.old"; verb=updated; fi
mv "$tmp" "$dest"
echo "sync-externals: $dest $verb from $img ($(find "$dest" -mindepth 1 -maxdepth 1 -printf '%f ' | sort))"
