#!/usr/bin/env bash
# tests/argv-secrets/run-noble.sh [workflow.yml]: run.sh inside ubuntu:24.04, i.e. on the SAME OpenSSL 3.0 as the
# mari runners (Ubuntu Noble). A newer dev-box OpenSSL masked a runner-only failure once (v2.1.4: a DER cert in a
# PEM bundle), so behaviour that depends on the OpenSSL version must be proven here. Needs docker.
set -euo pipefail
here=$(cd "$(dirname "$0")" && pwd); repo=$(cd "$here/../.." && pwd)
wf=${1:-.github/workflows/catboy-sign.yml}; case $wf in /*) mnt=(-v "$wf:/wf.yml:ro"); wf=/wf.yml ;; *) mnt=() ;; esac
exec docker run --rm --cap-add SYS_PTRACE --security-opt seccomp=unconfined -v "$repo:/repo:ro" "${mnt[@]}" -w /repo ubuntu:24.04 bash -c '
  set -e; export DEBIAN_FRONTEND=noninteractive
  apt-get update -qq >/dev/null && apt-get install -y -qq strace openssl jq curl python3 python3-yaml >/dev/null
  echo "== $(openssl version)"
  tests/argv-secrets/run.sh "$0"' "$wf"
