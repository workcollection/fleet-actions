#!/usr/bin/env bash
# Remove every container this job started with db-service (names collected in FLEET_DB_NAMES).
set -uo pipefail
if [ -z "${FLEET_DB_NAMES:-}" ]; then echo "no db-service container started in this job"; exit 0; fi
for n in $FLEET_DB_NAMES; do docker rm -f "$n" >/dev/null 2>&1 && echo "removed $n" || echo "$n already gone"; done
