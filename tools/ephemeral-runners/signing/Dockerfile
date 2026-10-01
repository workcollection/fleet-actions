# Runner image for the dedicated signing pool: the general runner image plus the tools
# catboy-sign.yml needs (curl, jq, openssl are already in the base; osslsigncode is not).
# Pin the base by digest; build on the signing host: docker build -t pn-sign-runner:1 .
ARG BASE=myoung34/github-runner:ubuntu-noble
FROM ${BASE}
RUN apt-get update \
 && apt-get install -y --no-install-recommends osslsigncode \
 && rm -rf /var/lib/apt/lists/* \
 && command -v curl jq openssl osslsigncode
