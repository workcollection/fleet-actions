#!/usr/bin/env bash
# Start a throwaway Postgres that the job reaches on 127.0.0.1. Called by action.yml; inputs come in as PG_* env.
set -euo pipefail
label=org.catboy.fleet-pg
# A killed run never reaches its stop step: remove this action's containers older than PG_STALE_S.
now=$(date +%s)
docker ps -a --filter "label=$label" --format '{{.ID}} {{.CreatedAt}}' | while read -r id d t z _; do
  c=$(date -d "$d $t $z" +%s 2>/dev/null) || continue
  if [ $(( now - c )) -gt "$PG_STALE_S" ]; then echo "removing stale postgres $id (created $d $t $z)"; docker rm -f "$id" >/dev/null || true; fi
done
# Random suffix: matrix legs of one run can share a daemon.
name="fleet-pg-${GITHUB_RUN_ID:-local}-${GITHUB_RUN_ATTEMPT:-0}-$RANDOM$RANDOM"
envs=(-e "POSTGRES_USER=$PG_USER" -e "POSTGRES_PASSWORD=$PG_PASSWORD" -e "POSTGRES_DB=$PG_DB")
self=""
for c in "$(hostname)" "$(grep -oE '/docker/containers/[0-9a-f]{64}' /proc/self/mountinfo | head -1 | cut -d/ -f4 || true)"; do
  if [ -n "$c" ] && docker inspect "$c" >/dev/null 2>&1; then self=$c; break; fi
done
if [ -n "$self" ]; then
  echo "runner is container $self: postgres shares its network namespace"
  docker run -d --name "$name" --label "$label=1" --network "container:$self" "${envs[@]}" "$PG_IMAGE" >/dev/null
  port=5432
else
  echo "runner is not a container on this daemon: publishing an ephemeral port"
  docker run -d --name "$name" --label "$label=1" -p 127.0.0.1::5432 "${envs[@]}" "$PG_IMAGE" >/dev/null
  port=$(docker port "$name" 5432/tcp | head -1 | sed 's/.*://')
fi
echo "FLEET_PG_NAME=$name" >> "$GITHUB_ENV"
for i in $(seq 1 "$PG_TIMEOUT_S"); do
  if docker exec "$name" pg_isready -U "$PG_USER" -h 127.0.0.1 -q && (exec 3<>"/dev/tcp/127.0.0.1/$port") 2>/dev/null; then
    url="postgres://$PG_USER:$PG_PASSWORD@127.0.0.1:$port/$PG_DB?sslmode=disable"
    echo "postgres ready on 127.0.0.1:$port after ${i}s ($PG_ENV_NAME set)"
    echo "$PG_ENV_NAME=$url" >> "$GITHUB_ENV"
    if [ "$PG_REQUIRE" = true ]; then echo "REQUIRE_DB_TESTS=1" >> "$GITHUB_ENV"; fi
    { echo "url=$url"; echo "port=$port"; echo "name=$name"; } >> "$GITHUB_OUTPUT"
    exit 0
  fi
  sleep 1
done
echo "postgres not reachable from the job on 127.0.0.1:$port after ${PG_TIMEOUT_S}s"
docker logs "$name" 2>&1 | tail -20
exit 1
