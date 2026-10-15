#!/usr/bin/env bash
# Start a throwaway database/cache container that the job reaches on 127.0.0.1. Called by action.yml
# (inputs arrive as DB_* env). Engines: postgres, mariadb, mysql, redis.
set -euo pipefail
label=org.catboy.fleet-db
case "$DB_ENGINE" in
  postgres) port_in=5432; image_def=public.ecr.aws/docker/library/postgres:16;  fb_def=mirror.gcr.io/library/postgres:16
            envs=(-e "POSTGRES_USER=$DB_USER" -e "POSTGRES_PASSWORD=$DB_PASSWORD" -e "POSTGRES_DB=$DB_NAME")
            ready=(pg_isready -U "$DB_USER" -h 127.0.0.1 -q); scheme=postgres; q='?sslmode=disable'; env_def=DATABASE_URL ;;
  mariadb)  port_in=3306; image_def=public.ecr.aws/docker/library/mariadb:11;   fb_def=mirror.gcr.io/library/mariadb:11
            envs=(-e "MARIADB_USER=$DB_USER" -e "MARIADB_PASSWORD=$DB_PASSWORD" -e "MARIADB_DATABASE=$DB_NAME" -e "MARIADB_ROOT_PASSWORD=$DB_PASSWORD")
            ready=(mariadb -h 127.0.0.1 -u "$DB_USER" "-p$DB_PASSWORD" -e 'select 1' "$DB_NAME"); scheme=mysql; q=''; env_def=DATABASE_URL ;;
  mysql)    port_in=3306; image_def=public.ecr.aws/docker/library/mysql:8.0;     fb_def=mirror.gcr.io/library/mysql:8.0
            envs=(-e "MYSQL_USER=$DB_USER" -e "MYSQL_PASSWORD=$DB_PASSWORD" -e "MYSQL_DATABASE=$DB_NAME" -e "MYSQL_ROOT_PASSWORD=$DB_PASSWORD")
            ready=(mysql -h 127.0.0.1 -u "$DB_USER" "-p$DB_PASSWORD" -e 'select 1' "$DB_NAME"); scheme=mysql; q=''; env_def=DATABASE_URL ;;
  redis)    port_in=6379; image_def=public.ecr.aws/docker/library/redis:7-alpine; fb_def=mirror.gcr.io/library/redis:7-alpine
            envs=(); ready=(sh -c 'redis-cli -h 127.0.0.1 ping | grep -qx PONG'); scheme=redis; q=''; env_def=REDIS_URL ;;
  *) echo "unknown engine '$DB_ENGINE' (postgres, mariadb, mysql, redis)"; exit 1 ;;
esac
image=${DB_IMAGE:-$image_def}; fallback=${DB_FALLBACK_IMAGE:-$fb_def}; [ "$fallback" = none ] && fallback=""; env_name=${DB_ENV_NAME:-$env_def}
self=""
for c in "$(hostname)" "$(grep -oE '/docker/containers/[0-9a-f]{64}' /proc/self/mountinfo | head -1 | cut -d/ -f4 || true)"; do
  if [ -n "$c" ] && docker inspect "$c" >/dev/null 2>&1; then self=$(docker inspect -f '{{.Id}}' "$c"); break; fi
done
# Leftovers of killed runs (their stop step never ran): any of ours older than DB_STALE_S, and, on a
# runner container, any of ours in its network namespace that this job did not start (a runner runs
# one job at a time, so they belong to an earlier job and would hold the port). FLEET_DB_NAMES is
# job-scoped (GITHUB_ENV), so a second service of this job keeps the first.
now=$(date +%s)
docker ps -a --filter "label=$label" --format '{{.ID}} {{.Names}} {{.CreatedAt}}' | while read -r id n d t z _; do
  case " ${FLEET_DB_NAMES:-} " in *" $n "*) continue ;; esac
  c=$(date -d "$d $t $z" +%s 2>/dev/null) || continue
  mode=$(docker inspect -f '{{.HostConfig.NetworkMode}}' "$id" 2>/dev/null || true)
  if [ $(( now - c )) -gt "$DB_STALE_S" ] || { [ -n "$self" ] && [ "$mode" = "container:$self" ]; }; then
    echo "removing leftover $id (created $d $t $z, network $mode)"; docker rm -f "$id" >/dev/null || true; fi
done
# Pull first, with retries and a second registry: a shared runner IP can exhaust the anonymous
# limit of one mirror ("toomanyrequests: Data limit exceeded" from ECR Public on hosted runners).
img=""
for try in 1 2 3; do
  for c in $image $fallback; do
    if docker image inspect "$c" >/dev/null 2>&1 || docker pull -q "$c" >/dev/null; then img=$c; break 2; fi
    echo "pull of $c failed (round $try)"
  done
  [ "$try" -lt 3 ] && sleep $(( 5 * try ))
done
[ -n "$img" ] || { echo "no $DB_ENGINE image could be pulled from: $image $fallback"; exit 1; }
echo "image: $img"
# Random suffix: matrix legs of one run can share a daemon.
name="fleet-$DB_ENGINE-${GITHUB_RUN_ID:-local}-${GITHUB_RUN_ATTEMPT:-0}-$RANDOM$RANDOM"
if [ -n "$self" ]; then
  echo "runner is container ${self:0:12}: $DB_ENGINE shares its network namespace"
  docker run -d --name "$name" --label "$label=1" --network "container:$self" "${envs[@]}" "$img" >/dev/null
  port=$port_in
else
  echo "runner is not a container on this daemon: publishing an ephemeral port"
  docker run -d --name "$name" --label "$label=1" -p "127.0.0.1::$port_in" "${envs[@]}" "$img" >/dev/null
  port=$(docker port "$name" "$port_in/tcp" | head -1 | sed 's/.*://')
fi
echo "FLEET_DB_NAMES=${FLEET_DB_NAMES:+$FLEET_DB_NAMES }$name" >> "$GITHUB_ENV"
for i in $(seq 1 "$DB_TIMEOUT_S"); do
  # The image's init phase runs a temporary server without TCP, so a TCP connect from the job
  # also proves the final server is up.
  if docker exec "$name" "${ready[@]}" >/dev/null 2>&1 && (exec 3<>"/dev/tcp/127.0.0.1/$port") 2>/dev/null; then
    if [ "$DB_ENGINE" = redis ]; then url="redis://127.0.0.1:$port"; else url="$scheme://$DB_USER:$DB_PASSWORD@127.0.0.1:$port/$DB_NAME$q"; fi
    echo "$DB_ENGINE ready on 127.0.0.1:$port after ${i}s ($env_name set)"
    echo "$env_name=$url" >> "$GITHUB_ENV"
    if [ "$DB_REQUIRE" = true ] && [ "$DB_ENGINE" != redis ]; then echo "REQUIRE_DB_TESTS=1" >> "$GITHUB_ENV"; fi
    { echo "url=$url"; echo "host=127.0.0.1"; echo "port=$port"; echo "name=$name"; } >> "$GITHUB_OUTPUT"
    exit 0
  fi
  sleep 1
done
echo "$DB_ENGINE not reachable from the job on 127.0.0.1:$port after ${DB_TIMEOUT_S}s"
docker logs "$name" 2>&1 | tail -20
exit 1
