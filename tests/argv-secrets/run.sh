#!/usr/bin/env bash
# tests/argv-secrets/run.sh [workflow.yml]: runs catboy-sign steps 2 (login), 3 (cert),
# 5 (shred/revoke) and 8 (report) VERBATIM against stub.py (fake OpenBao/GitHub OIDC/CatCMDB/CRL)
# under `strace -f -e execve`. It prints how often each secret appears in ANY exec'd argv (must be 0)
# and which secrets each endpoint received (proves delivery). Needs strace, openssl, jq, python3+yaml.
set -u
here=$(cd "$(dirname "$0")" && pwd); WF=$(realpath "${1:-$here/../../.github/workflows/catboy-sign.yml}")
W=$(mktemp -d); trap 'rm -rf "$W"' EXIT; cp "$here/stub.py" "$W/"; cd "$W" || exit 1; mkdir -p rt ca; touch ca/index.txt
python3 -c "
import yaml,sys; w=yaml.safe_load(open(sys.argv[1]))
for i in (2,3,5,8): open(f'step{i}.sh','w').write(w['jobs']['sign']['steps'][i]['run'])" "$WF"
# throwaway PKI: root -> intermediate (CDP) -> code-signing leaf (CDP), plus a real CRL for the gate
printf '[ca]\ndefault_ca=d\n[d]\ndatabase=ca/index.txt\ndefault_md=sha256\ndefault_crl_days=1\n' > ca.cnf
printf 'basicConstraints=critical,CA:TRUE\nkeyUsage=critical,keyCertSign,cRLSign\ncrlDistributionPoints=URI:http://127.0.0.1:18790/crl/x.crl\n' > int.ext
printf 'extendedKeyUsage=codeSigning\ncrlDistributionPoints=URI:http://127.0.0.1:18790/crl/x.crl\n' > leaf.ext
k() { openssl req -newkey ec -pkeyopt ec_paramgen_curve:P-256 -nodes -keyout "$1.key" -out "$1.csr" -subj "/CN=harness-$1" 2>/dev/null; }
k root; openssl x509 -req -in root.csr -key root.key -out root.pem -days 2 -extfile <(printf 'basicConstraints=critical,CA:TRUE\nkeyUsage=critical,keyCertSign,cRLSign\n') 2>/dev/null
k int; openssl x509 -req -in int.csr -CA root.pem -CAkey root.key -CAcreateserial -out int.pem -days 1 -extfile int.ext 2>/dev/null
k leaf; openssl x509 -req -in leaf.csr -CA int.pem -CAkey int.key -CAcreateserial -out leaf.pem -days 1 -extfile leaf.ext 2>/dev/null
openssl ca -config ca.cnf -gencrl -keyfile int.key -cert int.pem -out x.crl.pem 2>/dev/null; openssl crl -in x.crl.pem -outform DER -out x.crl
python3 -c "
import json, secrets
json.dump({k: 'SECRET' + k + secrets.token_hex(8) for k in ['OIDC_JWT','BAO_TOKEN','RUNNER_TOKEN','SECRET_ID','CATCMDB_TOKEN','ID_REQ_TOKEN']}, open('secrets.json','w'))
json.dump({'root': open('int.pem').read(), 'leaf': open('leaf.pem').read(), 'key': open('leaf.key').read()}, open('pki.json','w'))"
S() { python3 -c "import json;print(json.load(open('secrets.json'))['$1'])"; }
python3 stub.py & STUB=$!; sleep 0.7
printf %s "$(S SECRET_ID)" > rt/secret_id; printf %s "$(S CATCMDB_TOKEN)" > rt/catcmdb_token; chmod 600 rt/*
export RUNNER_TEMP=$PWD/rt GITHUB_OUTPUT=$PWD/rt/out GITHUB_ENV=$PWD/rt/env GITHUB_STEP_SUMMARY=$PWD/rt/sum
ACTIONS_ID_TOKEN_REQUEST_TOKEN=$(S ID_REQ_TOKEN); export ACTIONS_ID_TOKEN_REQUEST_URL="http://127.0.0.1:18790/oidc?x=1" ACTIONS_ID_TOKEN_REQUEST_TOKEN
export CATBOY_BAO_ADDR=http://127.0.0.1:18790 CATBOY_RUNNER_ISSUER=pve-r1 CATBOY_BAO_ROLE_ID=role-id-not-secret
export CATBOY_BAO_SECRET_ID_FILE=$PWD/rt/secret_id CATCMDB_URL=http://127.0.0.1:18790 CATCMDB_TOKEN_FILE=$PWD/rt/catcmdb_token
export CATBOY_PKI_CODESIGN_MOUNT=pki-stg-codesign CATBOY_PKI_ROLE_PREFIX=sign-stg CATBOY_PKI_BASE=http://127.0.0.1:18790
export GITHUB_REPOSITORY=smol-kitten/harness GITHUB_REPOSITORY_OWNER=smol-kitten GITHUB_REPOSITORY_ID=424242 GITHUB_RUN_ID=1 GITHUB_SHA=abc GITHUB_REF=refs/tags/v1 GITHUB_WORKFLOW_REF=x RUNNER_NAME=runner-pve-r1
PINNED_ROOTS="staging $(openssl x509 -in root.pem -outform DER | base64 -w0)"; export REQUIRE_PRODUCTION=false PINNED_ROOTS
touch rt/env
for i in 2 3 5 8; do
  if [ $i = 8 ]; then export G0_GATE=2 G0_REASON=harness-unsigned STRICT=false LOGIN_OUTCOME=success CERT_OUTCOME=failure CERT_REASON=x CERT_SERIAL="" HIERARCHY="" ROOT_FP="" SIGN_OUTCOME=skipped SIGN_GATE="" SIGN_COUNTS="" UPLOAD_OUTCOME=skipped ARTIFACT_NAME=dist; fi
  set -a; . rt/env 2>/dev/null; set +a      # what GitHub does with GITHUB_ENV between steps
  strace -f -qq -e trace=execve -s 100000 -o trace.$i bash --noprofile --norc -eo pipefail step$i.sh > rt/log.$i 2>&1; echo "step $i exit $?  ($(grep -c execve trace.$i) execs)"
done
# step 8 again for a caller without id-token: write: it must still report, just without X-GitHub-OIDC
env -u ACTIONS_ID_TOKEN_REQUEST_URL -u ACTIONS_ID_TOKEN_REQUEST_TOKEN strace -f -qq -e trace=execve -s 100000 -o trace.8b \
  bash --noprofile --norc -eo pipefail step8.sh > rt/log.8b 2>&1; echo "step 8 (no id-token) exit $?"
kill $STUB
echo "== secrets on ANY execve argv (all processes, all steps):"
for k in $(python3 -c "import json;print(' '.join(json.load(open('secrets.json'))))"); do
  v=$(S $k); echo "  $k: $(cat trace.* | grep -c -F "$v")"; done
echo "== processes exec'd:"; cat trace.* | grep -o 'execve("[^"]*' | sed 's/execve("//' | xargs -n1 basename | sort | uniq -c | sort -rn | head -20 | tr '\n' ' '; echo
fail=$(for k in $(python3 -c "import json;print(' '.join(json.load(open('secrets.json'))))"); do cat trace.* | grep -c -F "$(S $k)"; done | awk "{s+=\$1} END{print s+0}")
echo "== what the stub received (secret names per request):"; python3 -c "
import json; [print('  ',r['path'][:60], r['got']) for r in json.load(open('seen.json'))]"
echo "TOTAL secret occurrences on argv: $fail"; [ "$fail" = 0 ]
