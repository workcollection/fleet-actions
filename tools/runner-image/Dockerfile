# Fleet runner image: the pinned myoung34 runner base plus osslsigncode built from source.
# One image, two uses (it carries NO signing identity; that is mounted at run time):
#   - the long-lived mari pool on n4 CT113:   docker build -t fleet-runner:noble-oss214 .
#   - the ephemeral signing pool (CT 118):    docker build -t pn-sign-runner:1 .
# curl, jq and openssl are in the base. osslsigncode is built from source because Ubuntu
# noble ships 2.8 and catboy-sign needs >= 2.13 (from 2.1.7 it gates below that).
ARG BASE=myoung34/github-runner@sha256:91d2574e601b66d64ed987b76aea9bf565d4cd31da703ddfc0ec9c6b4ed446e9
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
COPY --from=osslsigncode /usr/local/bin/osslsigncode /usr/local/bin/osslsigncode
RUN command -v curl jq openssl osslsigncode \
 && osslsigncode --version | grep -q "osslsigncode ${OSSLSIGNCODE_VERSION}," \
 && osslsigncode --version | head -1
