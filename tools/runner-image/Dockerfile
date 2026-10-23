# Fleet runner image: the pinned myoung34 runner base plus osslsigncode built from source.
# One image, two uses (it carries NO signing identity; that is mounted at run time):
#   - the long-lived mari pool on n4 CT113:   docker build -t fleet-runner:noble-oss214 .
#   - the ephemeral signing pool (CT 118):    docker build -t pn-sign-runner:1 .
# curl, jq and openssl are in the base. osslsigncode is built from source because Ubuntu
# noble ships 2.8 and catboy-sign needs >= 2.13 (from 2.1.7 it gates below that).
# Base = 2.338.1-ubuntu-noble: actions-runner 2.338.1, externals node24 24.22.1 (Node2Shell fix).
ARG BASE=myoung34/github-runner@sha256:36782fe0e5994b301652408c32113a3562fabb1f0ead865ccc196d3e870f9109
ARG RUNNER_VERSION=2.338.1
ARG NODE_VERSION=24.22.1
ARG NODE_SHA256=6c172a9ab6718f7b801fb5a7bd486af2ee5e32758086a04b84fcce2aa77095e6
ARG OSSLSIGNCODE_VERSION=2.14
ARG OSSLSIGNCODE_SHA256=0f033fd6069387d2e489fbd2187e62f624764eb8c2758ee94e3e793e5150b5c5

FROM ${BASE} AS osslsigncode
ARG OSSLSIGNCODE_VERSION
ARG OSSLSIGNCODE_SHA256
RUN apt-get update \
 && apt-get install -y --no-install-recommends build-essential cmake libssl-dev libcurl4-openssl-dev zlib1g-dev ca-certificates curl \
 && rm -rf /var/lib/apt/lists/*
WORKDIR /src
# The tag archive is pinned by its sha256 (the release ships no source tarball).
RUN curl -fsSL -o osslsigncode.tar.gz "https://github.com/mtrojnar/osslsigncode/archive/refs/tags/${OSSLSIGNCODE_VERSION}.tar.gz" \
 && echo "${OSSLSIGNCODE_SHA256}  osslsigncode.tar.gz" | sha256sum -c - \
 && tar xzf osslsigncode.tar.gz --strip-components=1 \
 && cmake -S . -B build -DCMAKE_BUILD_TYPE=Release \
 && cmake --build build -j"$(nproc)" \
 && install -m 0755 build/osslsigncode /usr/local/bin/osslsigncode

FROM ${BASE}
# Local-only image (no registry has it): keep it out of `docker image prune -a`, which deletes it
# whenever no container uses it, e.g. while a gate has parked every runner (2026-10-04: a pool was down 59 h).
LABEL org.catboy.keep="true"
ARG OSSLSIGNCODE_VERSION
ARG RUNNER_VERSION
ARG NODE_VERSION
ARG NODE_SHA256
COPY --from=osslsigncode /usr/local/bin/osslsigncode /usr/local/bin/osslsigncode
# Node2Shell (CVE-2026-111497): the base still puts apt nodejs 18.19.1 (EOL, unfixed) on PATH.
# Purge it and install the sha256-pinned official tarball into /usr/local instead.
RUN apt-get purge -y --auto-remove nodejs libnode109 node-acorn \
 && rm -rf /var/lib/apt/lists/* \
 && curl -fsSL -o /tmp/node.tar.xz "https://nodejs.org/dist/v${NODE_VERSION}/node-v${NODE_VERSION}-linux-x64.tar.xz" \
 && echo "${NODE_SHA256}  /tmp/node.tar.xz" | sha256sum -c - \
 && tar -xJf /tmp/node.tar.xz -C /usr/local --strip-components=1 --exclude=CHANGELOG.md --exclude=LICENSE --exclude=README.md \
 && rm /tmp/node.tar.xz \
 && ln -sf /usr/local/bin/node /usr/bin/node \
 && node --version | grep -qx "v${NODE_VERSION}"
# There is no fixed Node 20 release, and the runner still ships externals/node20 20.20.2. Point
# node20 at node24 so actions that declare `using: node20` run on the fixed 24.x. Auto-update is
# off in the launchers (DISABLE_AUTO_UPDATE=true), so the runner never downloads node20 back.
RUN cd /actions-runner/externals \
 && rm -rf node20 node20_alpine \
 && ln -s node24 node20 \
 && ln -s node24_alpine node20_alpine \
 && node20/bin/node --version | grep -qx "v${NODE_VERSION}" \
 && node24/bin/node --version | grep -qx "v${NODE_VERSION}" \
 && /actions-runner/bin/Runner.Listener --version | grep -qx "${RUNNER_VERSION}"
RUN command -v curl jq openssl osslsigncode \
 && osslsigncode --version | grep -q "osslsigncode ${OSSLSIGNCODE_VERSION}," \
 && osslsigncode --version | head -1
