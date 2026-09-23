#!/usr/bin/env bash
# Smoke Contract: boot the pinned images as a disposable project and prove a fresh
# install is usable. Passing this is what makes a version a Pinned Version.
# Needs Docker and the images (~6 GB). Uses its own project name, ports and data dir,
# so it can run beside an installed stack. Exit 0 = contract holds.
set -euo pipefail
root=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
cd "$root"

export COMPOSE_PROJECT_NAME=${SMOKE_PROJECT:-llm-gateway-smoke}
[[ "$COMPOSE_PROJECT_NAME" =~ ^llm-gateway-smoke(-[a-z0-9-]+)?$ ]] || { echo "refusing to run smoke as the installed project" >&2; exit 2; }
# Prevent shell overrides from attaching another installation's storage.
for key in $(compgen -e); do
  case "$key" in LG_*|LANGFUSE_*|RUSTFS_*|CLICKHOUSE_*|VALKEY_*|POSTGRES_*|LITELLM_*|UI_USERNAME|UI_PASSWORD|COMPOSE_FILE|COMPOSE_PROFILES|COMPOSE_ENV_FILES) unset "$key";; esac
done
export COMPOSE_FILE="$root/compose.yaml"
export LG_VOLUME_PREFIX="$COMPOSE_PROJECT_NAME"
volumes=()
for suffix in clickhouse-data clickhouse-logs rustfs-data valkey-data caddy-data caddy-config; do
  volumes+=("$LG_VOLUME_PREFIX-$suffix")
done
existing=$(docker volume ls --format '{{.Name}}')
for volume in "${volumes[@]}"; do
  if grep -Fxq "$volume" <<< "$existing"; then echo "smoke volume already exists: $volume" >&2; exit 2; fi
done
[[ -z "$(docker ps -aq --filter "label=com.docker.compose.project=$COMPOSE_PROJECT_NAME")" ]] || { echo "smoke project already exists" >&2; exit 2; }
SMOKE_PROJECT="$COMPOSE_PROJECT_NAME-access" python3 scripts/smoke-access.py
http_port=${SMOKE_HTTP_PORT:-18080}
https_port=${SMOKE_HTTPS_PORT:-18443}
network="$COMPOSE_PROJECT_NAME-platform"
# Disjoint from the installed Platform Network (172.30.0.0/24); Docker refuses overlapping subnets.
subnet=${SMOKE_PLATFORM_SUBNET:-172.31.$(( $(cksum <<< "$COMPOSE_PROJECT_NAME" | cut -d' ' -f1) % 256 )).0/24}
mkdir -p "$root/.scratch"
work=$(mktemp -d "$root/.scratch/smoke-XXXXXX")
# Python is already required and reports device IDs on both GNU and BSD hosts.
device_id() { python3 -c 'import os, sys; print(os.stat(sys.argv[1]).st_dev)' "$1"; }
work_device=$(device_id "$work") || { rmdir "$work"; exit 2; }
backup_root=${SMOKE_BACKUP_ROOT:-/tmp}
if [[ ! -d "$backup_root" || ! -w "$backup_root" ]]; then
  rmdir "$work"
  echo "Set SMOKE_BACKUP_ROOT to an existing writable directory on a separate filesystem." >&2
  exit 2
fi
backup_device=$(device_id "$backup_root") || { rmdir "$work"; exit 2; }
if [[ -z "${SMOKE_BACKUP_ROOT:-}" && -d /dev/shm && "$backup_device" == "$work_device" ]]; then
  backup_root=/dev/shm
  backup_device=$(device_id "$backup_root") || { rmdir "$work"; exit 2; }
fi
if [[ ! -w "$backup_root" || "$backup_device" == "$work_device" ]]; then
  rmdir "$work"
  echo "Set SMOKE_BACKUP_ROOT to a writable directory on a different filesystem from the checkout." >&2
  exit 2
fi
backup_work=$(mktemp -d "$backup_root/llm-gateway-smoke-XXXXXX")
pg_image=$(sed -n 's/^    image: [$]{LG_POSTGRES_IMAGE:-\(.*\)}/\1/p' compose.yaml)
[[ -n "$pg_image" ]] || { echo 'could not read the PostgreSQL image default' >&2; exit 2; }
env_file="$work/.env"
origin="localhost:$http_port"
pass=0
fail() { echo "FAIL: $*" >&2; exit 1; }
ok() { echo "ok: $*"; pass=$((pass + 1)); }

cleanup() {
  echo "tearing down $COMPOSE_PROJECT_NAME"
  if ! docker compose --env-file "$env_file" down -v --remove-orphans >/dev/null 2>&1; then
    echo "teardown failed; retained $work and volumes" >&2
    return 1
  fi
  for volume in "${volumes[@]}"; do
    docker volume rm "$volume" >/dev/null 2>&1 || true
  done
  # Postgres leaves root-owned files; remove them from inside a container.
  docker run --rm --network none --user 0 -v "$work:/w" -v "$backup_work:/backup" --entrypoint sh "$pg_image" -ec 'rm -rf /w/pg /backup/backups' >/dev/null 2>&1 || true
  docker network rm "$network" >/dev/null 2>&1 || true
  rm -rf "$work" "$backup_work"
}
trap cleanup EXIT

sed -e "s#^LG_POSTGRES_DATA_DIR=.*#LG_POSTGRES_DATA_DIR=$work/pg#" \
    -e "s#^LG_VOLUME_PREFIX=.*#LG_VOLUME_PREFIX=$LG_VOLUME_PREFIX#" \
    -e "s#^LG_BACKUP_DIR=.*#LG_BACKUP_DIR=$backup_work/backups#" \
    -e "s#^LG_HTTP_PORT=.*#LG_HTTP_PORT=$http_port#" \
    -e "s#^LG_HTTPS_PORT=.*#LG_HTTPS_PORT=$https_port#" \
    -e "s#^LG_PUBLIC_PORT_SUFFIX=.*#LG_PUBLIC_PORT_SUFFIX=:$http_port#" \
    -e "s#^LG_PLATFORM_NETWORK=.*#LG_PLATFORM_NETWORK=$network#" \
    -e "s#^LG_PLATFORM_SUBNET=.*#LG_PLATFORM_SUBNET=$subnet#" \
    -e "s#^LG_PLATFORM_IP_RANGE=.*#LG_PLATFORM_IP_RANGE=$subnet#" \
    -e "s#^LANGFUSE_INIT_USER_EMAIL=.*#LANGFUSE_INIT_USER_EMAIL=smoke@gateway.test#" \
    .env.example > "$env_file"
mkdir -p "$backup_work/backups"
chmod 755 "$backup_work/backups"

echo "booting $COMPOSE_PROJECT_NAME on $origin"
python3 scripts/bootstrap.py --env-file "$env_file" >/dev/null
ok "bootstrap reached readiness"

set -a
# shellcheck disable=SC1090
. "$env_file"
set +a
auth=(-H "Authorization: Bearer $LITELLM_MASTER_KEY" -H "Content-Type: application/json")
lf=(-u "$LANGFUSE_INIT_PROJECT_PUBLIC_KEY:$LANGFUSE_INIT_PROJECT_SECRET_KEY")

# The exact service set, exact health values, and caddy as the only publisher.
docker compose --env-file "$env_file" ps -a --format json | python3 -c '
import json, sys
expected = {"caddy": "healthy", "valkey-exporter": "", "postgres-exporter": "", "litellm": "healthy", "langfuse-web": "healthy", "langfuse-worker": "healthy",
            "clickhouse": "healthy", "rustfs": "healthy", "valkey": "healthy", "postgres": "healthy"}
seen = {}
for line in sys.stdin:
    c = json.loads(line)
    if c["Service"] == "rustfs-init":
        if c.get("ExitCode") != 0: sys.exit("rustfs-init exit " + str(c.get("ExitCode")))
        continue
    seen[c["Service"]] = c.get("Health", "")
    if c.get("State") != "running": sys.exit(c["Service"] + " is " + str(c.get("State")))
    ports = [p for p in c.get("Publishers") or [] if p.get("PublishedPort")]
    if ports and c["Service"] != "caddy": sys.exit(c["Service"] + " publishes " + str(ports))
if seen != expected: sys.exit("services/health differ: " + json.dumps(seen))' || fail "service set or health"
ok "all services healthy, only caddy published"

for service in clickhouse rustfs; do
  directory=/var/log/clickhouse-server
  [[ "$service" != rustfs ]] || directory=/logs
  files=$(docker compose --env-file "$env_file" exec -T "$service" find "$directory" -type f -size +0c)
  [[ -z "$files" ]] || fail "$service wrote application log files"
done
ok "ClickHouse and RustFS produce no application log files"

# Console and its data.
# Read the complete page before matching: bundled icons can exceed the pipe buffer.
curl -fsS "http://$origin/" -o "$work/console.html" || fail "console request failed"
grep -q 'LLM Gateway' "$work/console.html" || fail "console did not render"
ok "console served"

# Status v2: the bootstrap-written document, served publicly; the v1 route is gone.
python3 - "http://$origin" <<'PYSTATUS' || fail "status document or health paths"
import json, sys, urllib.error, urllib.request
base = sys.argv[1]
def get(path):
    try:
        with urllib.request.urlopen(base + path, timeout=10) as response:
            return response.status, response.headers, response.read()
    except urllib.error.HTTPError as error:
        return error.code, error.headers, error.read()
status, headers, body = get("/status.json")
assert status == 200 and headers["Content-Type"] == "application/json" and headers["Cache-Control"] == "no-store"
doc = json.loads(body)
assert set(doc) == {"contract", "stack", "configuredAt", "components", "features"}, sorted(doc)
assert doc["contract"] == 2 and doc["stack"] == "gateway", doc
ids = ["caddy", "litellm", "langfuse-web", "langfuse-worker", "postgres", "clickhouse", "valkey", "rustfs",
       "postgres-exporter", "valkey-exporter"]
assert [c["id"] for c in doc["components"]] == ids, doc["components"]
for c in doc["components"]:
    assert {"id", "name", "kind", "enabled", "image", "version", "health"} <= set(c) <= {
        "id", "name", "kind", "enabled", "image", "version", "health", "url"}, c
    assert c["enabled"] is True and c["version"] and "@" not in c["image"], c
    assert get(c["health"])[0] == (404 if c["id"] == "valkey" else 200), c["health"]
assert doc["features"] == {"backups": {"configured": True, "lastCheckpointAt": None}}, doc["features"]
assert get("/versions.json")[0] == 404
PYSTATUS
ok "status.json is Status v2, its health paths answer, /versions.json is gone"

# Completion, streaming, and a failure that must not 500.
body='{"model":"gateway-mock","messages":[{"role":"user","content":"smoke"}]}'
curl -fsS "http://litellm.$origin/v1/chat/completions" "${auth[@]}" -d "$body" | grep -q '"finish_reason":"stop"' || fail "completion"
curl -fsS -N "http://litellm.$origin/v1/chat/completions" "${auth[@]}" -d '{"model":"gateway-mock","stream":true,"messages":[{"role":"user","content":"smoke"}]}' | grep -q '^data: ' || fail "streaming"
code=$(curl -s -o /dev/null -w '%{http_code}' "http://litellm.$origin/v1/chat/completions" "${auth[@]}" -d '{"model":"no-such-model","messages":[{"role":"user","content":"x"}]}')
[[ "$code" == "400" || "$code" == "404" ]] || fail "unknown model returned $code"
code=$(curl -s -o /dev/null -w '%{http_code}' "http://litellm.$origin/v1/chat/completions" -H "Content-Type: application/json" -d "$body")
[[ "$code" == "401" ]] || fail "unauthenticated request returned $code"
ok "completion, streaming, error and auth paths"

# The trace reaches Langfuse through OTLP (v4 has no legacy ingestion).
found=""
for _ in $(seq 1 30); do
  if curl -fsS "${lf[@]}" "http://langfuse.$origin/api/public/v2/observations?limit=10" | grep -q '"litellm_request"'; then found=1; break; fi
  sleep 2
done
[[ -n "$found" ]] || fail "no litellm_request observation in Langfuse within 60s"
ok "trace visible in Langfuse observations v2"

# Metrics move after a request.
metrics() { docker compose --env-file "$env_file" exec -T litellm python3 -c "import urllib.request; print(urllib.request.urlopen('http://localhost:4000/metrics').read().decode())"; }
metrics | grep -q '^litellm_' || fail "no litellm_ metrics"
ok "litellm /metrics exposed"

docker compose --env-file "$env_file" exec -T litellm python3 - <<'PYCODE'
import urllib.request
for host, port, required in (
    ('valkey-exporter', 9121, ('redis_up 1', 'redis_memory_used_bytes', 'redis_memory_max_bytes')),
    ('postgres-exporter', 9187, ('pg_up 1', 'pg_stat_archiver_failed_count')),
):
    text = urllib.request.urlopen(f'http://{host}:{port}/metrics', timeout=10).read().decode()
    for metric in required:
        if metric not in text:
            raise SystemExit(f'{host}: missing {metric}')
PYCODE
ok "Valkey and PostgreSQL exporters reach their stores and expose operational metrics"


# Queue survives a restart: work enqueued just before the restart still reaches Langfuse.
marker="smoke-$(date +%s)-$RANDOM"
curl -fsS "http://litellm.$origin/v1/chat/completions" "${auth[@]}" -d "{\"model\":\"gateway-mock\",\"messages\":[{\"role\":\"user\",\"content\":\"before-restart\"}],\"user\":\"$marker\"}" >/dev/null || fail "completion before valkey restart"
docker compose --env-file "$env_file" restart valkey >/dev/null
found=""
for _ in $(seq 1 45); do
  if curl -fsS "${lf[@]}" "http://langfuse.$origin/api/public/v2/observations?limit=50" | grep -q "\"$marker\""; then found=1; break; fi
  sleep 2
done
[[ -n "$found" ]] || fail "observation for user $marker not ingested after valkey restart"
ok "work queued before a valkey restart is ingested after it"

# Object storage: Langfuse wrote events, and a presigned URL round-trips through Caddy
# the way a browser fetches media. The init service's entrypoint is `sh -ec`, so the
# command is the script body.
s3() { docker compose --env-file "$env_file" run --rm --no-deps -T rustfs-init "aws configure set default.s3.addressing_style path >/dev/null; $*"; }
objects=$(s3 'aws --endpoint-url http://rustfs:9000 s3api list-objects-v2 --bucket langfuse --prefix events/ --max-keys 5 --query "length(Contents)" --output text' 2>/dev/null || true)
[[ "$objects" =~ ^[1-9][0-9]*$ ]] || fail "expected event objects in RustFS, got '$objects'"
s3 "printf '%s' '$marker' | aws --endpoint-url http://rustfs:9000 s3 cp - s3://langfuse/smoke/$marker.txt" >/dev/null || fail "s3 put"
url=$(s3 "aws --endpoint-url http://s3.$origin s3 presign s3://langfuse/smoke/$marker.txt --expires-in 300" | tr -d '\r')
[[ "$url" == http://s3.$origin/* ]] || fail "presigned URL has the wrong origin: $url"
body=$(curl -fsS "$url") || fail "presigned GET through Caddy"
[[ "$body" == "$marker" ]] || fail "presigned GET returned different bytes"
ok "events persisted in RustFS; presigned GET round-trips through Caddy"

# Browser media uploads: the preflight for a presigned PUT allows the Langfuse origin only.
allow=$(curl -s -o /dev/null -D - -X OPTIONS "http://s3.$origin/langfuse/media/x.png" -H "Origin: http://langfuse.$origin" -H "Access-Control-Request-Method: PUT" | tr -d '\r' | grep -i '^access-control-allow-origin:' || true)
[[ "$allow" == *"http://langfuse.$origin"* ]] || fail "CORS preflight did not allow the Langfuse origin: '$allow'"
foreign=$(curl -s -o /dev/null -D - -X OPTIONS "http://s3.$origin/langfuse/media/x.png" -H "Origin: http://evil.example" -H "Access-Control-Request-Method: PUT" | grep -i -c '^access-control-allow-origin' || true)
[[ "$foreign" == "0" ]] || fail "CORS preflight allowed a foreign origin"
ok "S3 CORS allows the Langfuse origin only"

echo "SMOKE CONTRACT PASSED ($pass checks)"
