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
# The one Platform Network peer allowed metrics: the subnet's last host, clear of dynamic allocation.
scraper_ip=$(python3 -c 'import ipaddress, sys; print(ipaddress.ip_network(sys.argv[1]).broadcast_address - 1)' "$subnet")
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
caddy_image=$(sed -n 's/^    image: [$]{LG_CADDY_IMAGE:-\(.*\)}/\1/p' compose.yaml)
[[ -n "$caddy_image" ]] || { echo 'could not read the Caddy image default' >&2; exit 2; }
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
    -e "s#^LG_CHECKPOINT_ALLOW=.*#LG_CHECKPOINT_ALLOW=\"127.0.0.0/8 ::1 $scraper_ip\"#" \
    -e "s#^LG_METRICS=.*#LG_METRICS=true#" \
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

# One access policy: the applications' own logins gate their admin surfaces, health bodies
# stay empty for everyone, and the sibling probes are gone. Docker presents the bridge
# address as the peer, never loopback.
code=$(curl -s -o "$work/ui.html" -w '%{http_code}' "http://litellm.$origin/ui/")
[[ "$code" == 200 ]] || fail "/ui/ answered $code"
grep -qi 'litellm' "$work/ui.html" || fail "/ui/ did not return the LiteLLM login page"
code=$(curl -s -o /dev/null -w '%{http_code}' "http://rustfs.$origin/rustfs/console/")
[[ "$code" == 200 ]] || fail "RustFS console answered $code"
for probe in "$origin/health/litellm" "litellm.$origin/health/readiness"; do
  # The body precedes the status code, so an empty 200 answer prints exactly "200".
  answer=$(curl -sS -w '%{http_code}' "http://$probe")
  [[ "$answer" == 200 ]] || fail "$probe answered '$answer'; expected status 200 with an empty body"
done
for sibling in backplane grafana; do
  code=$(curl -s -o /dev/null -w '%{http_code}' "http://$origin/health/$sibling")
  [[ "$code" == 404 ]] || fail "/health/$sibling answered $code"
done
ok "LiteLLM UI and RustFS console answer behind their own logins; health bodies empty; sibling probes 404"

# LiteLLM is reachable only through the gateway: no lg-litellm alias on the Platform Network.
if resolved=$(docker run --rm --network "$network" --entrypoint wget "$caddy_image" -qO- -T 5 http://lg-litellm:4000/health/readiness 2>&1); then
  fail "lg-litellm answered on the Platform Network: $resolved"
fi
[[ "$resolved" == *"bad address"* ]] || fail "lg-litellm failed for another reason than name resolution: $resolved"
ok "lg-litellm does not resolve on the Platform Network"

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

# LiteLLM metrics reach an allowed Platform Network peer through the gateway, after a request.
scrape() { docker run --rm --network "$network" "$@" --entrypoint wget "$caddy_image" -qO- http://lg-gateway:8081/metrics/litellm; }
scrape --ip "$scraper_ip" > "$work/litellm.prom" || fail "allowed peer could not read lg-gateway:8081/metrics/litellm"
grep -q '^# TYPE litellm_' "$work/litellm.prom" || fail "no litellm_ Prometheus text"
grep -q '^litellm_' "$work/litellm.prom" || fail "no litellm_ samples after a request"
denied=$(scrape 2>&1 >/dev/null || true)
[[ "$denied" == *" 404 "* ]] || fail "a peer outside LG_CHECKPOINT_ALLOW was not refused with 404: $denied"
ok "lg-gateway:8081/metrics/litellm serves Prometheus text to the allowed peer only"

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

# Operator certificate files: a throwaway CA signs one leaf for every Local Mode HTTPS name.
# OpenSSL rejects a wildcard directly under a single-label domain such as *.localhost.
mkdir "$work/certs"
printf 'basicConstraints=critical,CA:FALSE\nkeyUsage=critical,digitalSignature\nextendedKeyUsage=serverAuth\nsubjectAltName=DNS:localhost,DNS:litellm.localhost,DNS:langfuse.localhost,DNS:s3.localhost,DNS:rustfs.localhost\n' > "$work/leaf.cnf"
openssl req -x509 -newkey ec -pkeyopt ec_paramgen_curve:P-256 -noenc -keyout "$work/ca.key" -out "$work/ca.crt" \
  -subj '/CN=llm-gateway smoke CA' -days 2 -addext 'basicConstraints=critical,CA:TRUE' \
  -addext 'keyUsage=critical,keyCertSign,cRLSign' 2>/dev/null
openssl req -newkey ec -pkeyopt ec_paramgen_curve:P-256 -noenc -keyout "$work/certs/tls.key" -out "$work/leaf.csr" \
  -subj '/CN=localhost' 2>/dev/null
openssl x509 -req -in "$work/leaf.csr" -CA "$work/ca.crt" -CAkey "$work/ca.key" -CAcreateserial -out "$work/certs/tls.crt" \
  -days 2 -extfile "$work/leaf.cnf" 2>/dev/null
docker compose --env-file "$env_file" exec -T caddy cat /data/caddy/pki/authorities/local/root.crt > "$work/root.crt"
# Caddy reads the key as uid 0 without CAP_DAC_OVERRIDE; this throwaway key stays inside the
# private work directory. Without a shell COMPOSE_FILE, bootstrap records the overlay in the
# env file after the mode file.
chmod 0644 "$work/certs/tls.key"
unset COMPOSE_FILE
export LG_TLS_ISSUER=files LG_TLS_DIR="$work/certs" LG_TLS_CA="$work/ca.crt"
python3 scripts/bootstrap.py --env-file "$env_file" >/dev/null || fail "bootstrap with the files issuer"
# shellcheck disable=SC2016 # the recorded value keeps Compose's literal mode token
[[ "$(grep '^COMPOSE_FILE=' "$env_file" | tail -n 1)" == 'COMPOSE_FILE=compose.yaml:compose.${LG_ACCESS_MODE:-local}.yaml:compose.files.yaml' ]] \
  || fail "files issuer: env file does not record compose.files.yaml after the mode file"
ok "files issuer: bootstrap recorded the overlay and verified HTTPS readiness against LG_TLS_CA"
for target in localhost/health/litellm litellm.localhost/health/readiness; do
  host=${target%%/*}
  url="https://$host:$https_port/${target#*/}"
  code=$(curl --noproxy '*' --max-time 10 --cacert "$work/ca.crt" -sS -o /dev/null -w '%{http_code}' \
    --resolve "$host:$https_port:127.0.0.1" "$url") || fail "files issuer: $host handshake"
  [[ "$code" == 200 ]] || fail "files issuer: $url answered $code"
  if curl --noproxy '*' --max-time 10 --cacert "$work/root.crt" -sS -o /dev/null --resolve "$host:$https_port:127.0.0.1" \
      "$url" 2>/dev/null; then fail "files issuer: $host still serves the internal CA certificate"; fi
done
ok "files issuer: application hostnames verified by the operator CA only"

echo "SMOKE CONTRACT PASSED ($pass checks)"
