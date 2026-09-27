#!/usr/bin/env bash
# tests/argv-secrets/run.sh [workflow.yml]: runs catboy-sign steps 1 (gate 0), 2 (login), 3 (cert),
# 4 (sign), 5 (shred/revoke) and 8 (report) VERBATIM against stub.py (fake OpenBao over TLS, GitHub OIDC,
# CatCMDB, pki-web, and a real RFC 3161 TSA via `openssl ts -reply`) under `strace -f -e execve`.
# Asserts:
#   - no secret appears in ANY exec'd argv, and every endpoint received its secret;
#   - the cleanup revoked the OIDC-login token;
#   - every request carried the catboy-sign User-Agent (v2.1.4);
#   - the archive's .p7s carries a .p7s.tsr that verifies to the root (v2.1.4);
#   - an empty pe-glob is skipped cleanly (v2.1.4);
#   - pass 2: a repo issuer created in this run (ensure-repo-issuer 201) whose CRL 404s twice is retried
#     until published, not failed (v2.1.4).
# Needs strace, openssl, jq, python3+yaml.
set -u
here=$(cd "$(dirname "$0")" && pwd); WF=$(realpath "${1:-$here/../../.github/workflows/catboy-sign.yml}")
W=$(mktemp -d); [ -n "${KEEP:-}" ] || trap 'rm -rf "$W"' EXIT; echo "workdir $W"; cp "$here/stub.py" "$W/"; cd "$W" || exit 1; mkdir -p rt ca cat0; touch ca/index.txt cat0/index.txt
python3 -c "
import yaml,sys; w=yaml.safe_load(open(sys.argv[1]))
for i in (1,2,3,4,5,8): open(f'step{i}.sh','w').write(w['jobs']['sign']['steps'][i]['run'])" "$WF"
# throwaway PKI: root -> intermediate (CDP) -> code-signing leaf (CDP), a real CRL for the gate,
# and root -> T0 -> TSA leaf for RFC 3161
printf '[ca]\ndefault_ca=d\n[d]\ndatabase=ca/index.txt\ndefault_md=sha256\ndefault_crl_days=1\n' > ca.cnf
printf '[ca]\ndefault_ca=d\n[d]\ndatabase=cat0/index.txt\ndefault_md=sha256\ndefault_crl_days=1\n' > cat0.cnf
printf 'basicConstraints=critical,CA:TRUE\nkeyUsage=critical,keyCertSign,cRLSign\ncrlDistributionPoints=URI:http://127.0.0.1:18790/crl/x.crl\n' > int.ext
printf 'extendedKeyUsage=codeSigning\ncrlDistributionPoints=URI:http://127.0.0.1:18790/crl/x.crl\n' > leaf.ext
k() { openssl req -newkey ec -pkeyopt ec_paramgen_curve:P-256 -nodes -keyout "$1.key" -out "$1.csr" -subj "/CN=harness-$1" 2>/dev/null; }
ca_ext='basicConstraints=critical,CA:TRUE\nkeyUsage=critical,keyCertSign,cRLSign\n'
k root; openssl x509 -req -in root.csr -key root.key -out root.pem -days 2 -extfile <(printf "$ca_ext") 2>/dev/null
k int; openssl x509 -req -in int.csr -CA root.pem -CAkey root.key -CAcreateserial -out int.pem -days 1 -extfile int.ext 2>/dev/null
# a runner CA under the intermediate, like the real rn-* under CS0/CB0: ca_chain then holds >= 2 PEM certs,
# which is what made OpenSSL 3.0 drop certs from a DER+PEM bundle (a DER cert + ONE PEM cert still parses)
k rn; openssl x509 -req -in rn.csr -CA int.pem -CAkey int.key -CAcreateserial -out rn.pem -days 1 -extfile int.ext 2>/dev/null
k leaf; openssl x509 -req -in leaf.csr -CA rn.pem -CAkey rn.key -CAcreateserial -out leaf.pem -days 1 -extfile leaf.ext 2>/dev/null
# T0 under the intermediate, like the real T0 under CB0: the TSA chain then NEEDS both certs from -untrusted
# (with T0 directly under the root, OpenSSL 3.0 read the first DER cert of a mixed bundle and still passed)
k t0; openssl x509 -req -in t0.csr -CA int.pem -CAkey int.key -CAcreateserial -out t0.pem -days 1 -extfile <(printf "$ca_ext") 2>/dev/null
k tsa; openssl x509 -req -in tsa.csr -CA t0.pem -CAkey t0.key -CAcreateserial -out tsa.pem -days 1 -extfile <(printf 'extendedKeyUsage=critical,timeStamping\nkeyUsage=critical,digitalSignature\n') 2>/dev/null
openssl x509 -in t0.pem -outform DER -out t0.der   # pki-web serves DER
openssl ca -config cat0.cnf -gencrl -keyfile t0.key -cert t0.pem -out t0.crl.pem 2>/dev/null; openssl crl -in t0.crl.pem -outform DER -out t0.crl
printf '[tsa]\ndefault_tsa=t\n[t]\nserial=tsaserial\ncrypto_device=builtin\nsigner_digest=sha256\ndefault_policy=1.3.6.1.4.1.66963.1.1.99\ndigests=sha256\naccuracy=secs:1\nordering=no\ntsa_name=yes\ness_cert_id_chain=no\ness_cert_id_alg=sha256\n' > ts.cnf; echo 01 > tsaserial
# TLS for the OpenBao stub: its own CA, server cert for IP 127.0.0.1 (the workflow pins it via CACERT_B64)
k tlsca; openssl x509 -req -in tlsca.csr -key tlsca.key -out tlsca.pem -days 2 -extfile <(printf 'basicConstraints=critical,CA:TRUE\nkeyUsage=critical,keyCertSign\n') 2>/dev/null
k tls; openssl x509 -req -in tls.csr -CA tlsca.pem -CAkey tlsca.key -CAcreateserial -out tls.pem -days 1 -extfile <(printf 'subjectAltName=IP:127.0.0.1\nextendedKeyUsage=serverAuth\n') 2>/dev/null
openssl ca -config ca.cnf -gencrl -keyfile int.key -cert int.pem -out x.crl.pem 2>/dev/null; openssl crl -in x.crl.pem -outform DER -out x.crl
python3 -c "
import json, secrets
json.dump({k: 'SECRET' + k + secrets.token_hex(8) for k in ['OIDC_JWT','BAO_TOKEN','RUNNER_TOKEN','SECRET_ID','CATCMDB_TOKEN','ID_REQ_TOKEN']}, open('secrets.json','w'))
json.dump({'chain': [open('rn.pem').read(), open('int.pem').read()], 'leaf': open('leaf.pem').read(), 'key': open('leaf.key').read()}, open('pki.json','w'))"
S() { python3 -c "import json;print(json.load(open('secrets.json'))['$1'])"; }

setup_rt() {   # a fresh RUNNER_TEMP + GITHUB_ENV + dist/ for one pass
  rm -rf rt dist; mkdir -p rt dist; touch rt/env
  printf %s "$(S SECRET_ID)" > rt/secret_id; printf %s "$(S CATCMDB_TOKEN)" > rt/catcmdb_token; chmod 600 rt/*
  printf 'harness\n' > hello.txt; tar -czf dist/e2e.tar.gz hello.txt
}
run_step() {   # run_step <i> <trace-suffix>
  set -a; . rt/env 2>/dev/null; set +a      # what GitHub does with GITHUB_ENV between steps
  strace -f -qq -e trace=execve -s 100000 -o "trace.$1$2" bash --noprofile --norc -eo pipefail "step$1.sh" > "rt/log.$1" 2>&1
  echo "step $1 exit $?  ($(grep -c execve "trace.$1$2") execs)"
}
export CURL_HOME=$PWD/rt/catboy-curl CATBOY_SIGN_VERSION=2.1.4-harness   # step env + workflow env in the real job
export RUNNER_TEMP=$PWD/rt GITHUB_OUTPUT=$PWD/rt/out GITHUB_ENV=$PWD/rt/env GITHUB_STEP_SUMMARY=$PWD/rt/sum
ACTIONS_ID_TOKEN_REQUEST_TOKEN=$(S ID_REQ_TOKEN); export ACTIONS_ID_TOKEN_REQUEST_URL="http://127.0.0.1:18790/oidc?x=1" ACTIONS_ID_TOKEN_REQUEST_TOKEN
CATBOY_BAO_CACERT_B64=$(base64 -w0 < tlsca.pem); export CATBOY_BAO_CACERT_B64 CATBOY_BAO_ADDR=https://127.0.0.1:18791 CATBOY_RUNNER_ISSUER=pve-r1 CATBOY_BAO_ROLE_ID=role-id-not-secret
export CATBOY_BAO_SECRET_ID_FILE=$PWD/rt/secret_id CATCMDB_URL=http://127.0.0.1:18790 CATCMDB_TOKEN_FILE=$PWD/rt/catcmdb_token
export CATBOY_PKI_CODESIGN_MOUNT=pki-stg-codesign CATBOY_PKI_ROLE_PREFIX=sign-stg CATBOY_PKI_BASE=http://127.0.0.1:18790
export CATBOY_TSA_URL=http://127.0.0.1:18790/api/v1/timestamp CATBOY_TIMESTAMP_CA_NAME=t0
export GITHUB_REPOSITORY=smol-kitten/harness GITHUB_REPOSITORY_OWNER=smol-kitten GITHUB_REPOSITORY_ID=424242 GITHUB_RUN_ID=1 GITHUB_SHA=abc GITHUB_REF=refs/tags/v1 GITHUB_WORKFLOW_REF=x RUNNER_NAME=runner-pve-r1
PINNED_ROOTS="staging $(openssl x509 -in root.pem -outform DER | base64 -w0)"; export REQUIRE_PRODUCTION=false PINNED_ROOTS
export FETCH_OUTCOME=success ARTIFACT_NAME=dist LEAF_SERIAL=01 HIERARCHY=staging ROOT_FP=x PE_GLOB='' ARCHIVE_GLOB='*.tar.gz'

# ---- pass 1: the whole job
setup_rt; python3 stub.py & STUB=$!; sleep 0.7
for i in 1 2 3 4 5 8; do
  if [ $i = 8 ]; then export G0_GATE=2 G0_REASON=harness-unsigned STRICT=false LOGIN_OUTCOME=success CERT_OUTCOME=failure CERT_REASON=x CERT_SERIAL="" SIGN_OUTCOME=skipped SIGN_GATE="" SIGN_COUNTS="" UPLOAD_OUTCOME=skipped; fi
  run_step $i ""
done
# step 8 again for a caller without id-token: write: it must still report, just without X-GitHub-OIDC
env -u ACTIONS_ID_TOKEN_REQUEST_URL -u ACTIONS_ID_TOKEN_REQUEST_TOKEN -u GITHUB_REPOSITORY_ID strace -f -qq -e trace=execve -s 100000 -o trace.8b \
  bash --noprofile --norc -eo pipefail step8.sh > rt/log.8b 2>&1; echo "step 8 (no id-token) exit $?"
kill $STUB; wait $STUB 2>/dev/null; cp seen.json seen1.json; cp -r dist dist1   # pass 2 resets dist/
echo "== secrets on ANY execve argv (all processes, all steps):"
for k in $(python3 -c "import json;print(' '.join(json.load(open('secrets.json'))))"); do
  v=$(S $k); echo "  $k: $(cat trace.* | grep -c -F "$v")"; done
echo "== processes exec'd:"; cat trace.* | grep -o 'execve("[^"]*' | sed 's/execve("//' | xargs -n1 basename | sort | uniq -c | sort -rn | head -20 | tr '\n' ' '; echo
fail=$(for k in $(python3 -c "import json;print(' '.join(json.load(open('secrets.json'))))"); do cat trace.* | grep -c -F "$(S $k)"; done | awk "{s+=\$1} END{print s+0}")
echo "== what the stub received (secret names per request):"; python3 -c "
import json; [print('  ',r['path'][:60], r['got'], *(['repository_id=%r' % r['repository_id']] if 'repository_id' in r else [])) for r in json.load(open('seen1.json'))]"
echo "TOTAL secret occurrences on argv: $fail"
ok=0; chk() { if eval "$2"; then echo "  ok   $1"; else echo "  FAIL $1"; ok=1; fi; }
rv=$(python3 -c "import json; print(sum(1 for r in json.load(open('seen1.json')) if r['path'].endswith('/auth/token/revoke-self') and 'BAO_TOKEN' in r['got']))")
chk "no secret on any argv" '[ "$fail" = 0 ]'
chk "cleanup revoke-self with the BAO token: $rv (want 1)" '[ "$rv" = 1 ]'
badua=$(python3 -c "import json; print(sum(1 for r in json.load(open('seen1.json')) if not r['ua'].startswith('catboy-sign/2.1.4-harness (+https://pki.catboy.systems/cps)')))")
chk "every request carries the catboy-sign User-Agent ($badua without)" '[ "$badua" = 0 ]'
chk "sign step: signed=1 failed=0 removed=0" 'grep -q "signed=1 failed=0 removed=0" rt/log.4'
chk "empty pe-glob skipped (no find error)" '! grep -q "empty parentheses" rt/log.4'
chk "e2e.tar.gz.p7s.tsr verifies to the root" 'cat t0.pem rn.pem int.pem > tsa-untr.pem; openssl ts -verify -data dist1/e2e.tar.gz.p7s -in dist1/e2e.tar.gz.p7s.tsr -CAfile root.pem -untrusted tsa-untr.pem >/dev/null 2>&1'
chk "SIGNATURES.md names the .tsr" 'grep -q "e2e.tar.gz.p7s.tsr" dist1/SIGNATURES.md'
# pki-web serves DER: bundles fed to `openssl ts -verify -untrusted` / -TSA-CAfile must be pure PEM with every
# cert in them. OpenSSL 3.0 (the runners' Ubuntu Noble) cannot read a DER cert cat'ed into a PEM bundle; newer
# OpenSSL tolerates it, so check the files, not just the verify (e2e v0.1.0: every .tsr failed on the runners)
pem_ok() { [ -s "$1" ] && ! LC_ALL=C grep -q -P '[^\x09\x0a\x0d\x20-\x7e]' "$1" && [ "$(grep -c 'BEGIN CERTIFICATE' "$1")" -ge "$2" ]; }
chk "TSA bundles are pure PEM with all certs (tsa-untrusted >= 2, tsa-anchors >= 2)" 'pem_ok rt/tsa-untrusted.pem 2 && pem_ok rt/tsa-anchors.pem 2'

# ---- pass 2: a repo issuer created in THIS run (201) whose CRL is not published yet (2x 404: both
# chain certs share the CDP, so try 1 sees them; the gate must wait and pass on try 2)
setup_rt; STUB_ERI_CODE=201 STUB_CRL_MISSES=2 python3 stub.py & STUB=$!; sleep 0.7
export CATBOY_CRL_RETRY_SLEEP=1
for i in 1 2 3; do run_step $i ".retry"; done
kill $STUB; wait $STUB 2>/dev/null
chk "retry: new issuer flagged (CATBOY_NEW_ISSUER=1)" 'grep -q "^CATBOY_NEW_ISSUER=1" rt/env'
chk "retry: CRL gate waited (404s), then passed" 'grep -q "try 1/13" rt/log.3 && ! grep -q "CRL not published for" rt/log.3 && grep -q "^hierarchy=staging" rt/out'
exit $ok
