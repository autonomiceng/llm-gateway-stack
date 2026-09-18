#!/usr/bin/env bash
# Static validation: what CI runs on every push. No containers are started.
set -euo pipefail
root=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
cd "$root"

for tool in docker python3 shellcheck; do
  command -v "$tool" >/dev/null || { echo "missing tool: $tool" >&2; exit 1; }
done
docker compose version >/dev/null

work=$(mktemp -d)
trap 'rm -rf "$work"' EXIT

export LG_BACKUP_DIR="$work/backups" LANGFUSE_INIT_USER_EMAIL=validate@gateway.test
python3 scripts/bootstrap.py --env-file "$work/.env" --render-only >/dev/null
echo "env render: PASS"

docker compose --env-file "$work/.env" config -q
docker compose --env-file "$work/.env" config --format json > "$work/config.json"
python3 - "$work/config.json" <<'PY'
import json, sys
config = json.load(open(sys.argv[1]))
published = {name for name, svc in config["services"].items() if svc.get("ports")}
if published != {"caddy"}:
    sys.exit(f"only caddy may publish ports, found {sorted(published)}")
for name, svc in config["services"].items():
    image = svc.get("image", "")
    if "@sha256:" not in image or image.split("@")[0].endswith(":latest"):
        sys.exit(f"{name}: image must be pinned as tag@sha256, got {image}")
    if svc.get("env_file"):
        sys.exit(f"{name}: env_file is not allowed; list variables explicitly")
shared = {name for name, svc in config["services"].items() if "platform" in (svc.get("networks") or {})}
if shared != {"caddy", "litellm", "valkey-exporter", "postgres-exporter"}:
    sys.exit(f"only caddy, litellm and datastore exporters join the platform network, found {sorted(shared)}")
PY
echo "compose config: PASS"

for mode in "http localhost none" "https example.com acme" "https example.com internal" "http example.com none"; do
  read -r scheme domain issuer <<< "$mode"
  for console in off on; do
    for proxies in "" "172.30.0.0/24"; do
      docker run --rm -e "LG_RUSTFS_CONSOLE=$console" -e "LG_TRUSTED_PROXIES=$proxies" \
        -e "LG_OPERATOR_ALLOW=127.0.0.0/8 ::1" -e "LG_LISTEN_SCHEME=$scheme" -e "LG_PUBLIC_DOMAIN=$domain" -e "LG_TLS_ISSUER=$issuer" \
        -v "$root/docker/caddy/Caddyfile:/etc/caddy/Caddyfile:ro" \
        "$(docker compose --env-file "$work/.env" config --images | grep '^caddy')" \
        caddy validate --config /etc/caddy/Caddyfile >/dev/null || { echo "Caddy validation failed for $mode (console=$console, proxies=$proxies)" >&2; exit 1; }
    done
  done
done
echo "Caddyfile: PASS (16 configurations)"

mapfile -t scripts < <(git ls-files '*.sh')
shellcheck "${scripts[@]}"
echo "shellcheck: PASS"

mapfile -t py < <(git ls-files '*.py')
python3 -m py_compile "${py[@]}"
echo "python: PASS"
