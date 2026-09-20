#!/usr/bin/env bash
# Static validation: what CI runs on every push. Only disposable Caddy validation containers run.
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
    if svc.get("logging") != {"driver": "journald", "options": {"cache-disabled": "true"}}:
        sys.exit(f"{name}: journald with no Docker file cache is required")
shared = {name for name, svc in config["services"].items() if "platform" in (svc.get("networks") or {})}
if shared != {"caddy", "litellm", "valkey-exporter", "postgres-exporter"}:
    sys.exit(f"only caddy, litellm and datastore exporters join the platform network, found {sorted(shared)}")
PY
echo "compose config: PASS"

for mode in local public proxy; do
  LG_ACCESS_MODE=$mode docker compose --env-file "$work/.env" config --format json > "$work/$mode.json"
done
python3 - "$work" <<'PY'
import json, pathlib, sys
for mode in ("local", "public", "proxy"):
    services = json.loads((pathlib.Path(sys.argv[1]) / f"{mode}.json").read_text())["services"]
    gateway = services["caddy"]
    ports = {port["target"] for port in gateway["ports"]}
    assert ports == ({80} if mode == "proxy" else {80, 443}), (mode, ports)
    scheme = "http" if mode == "local" else "https"
    assert gateway["environment"]["LG_SCHEME"] == scheme
    assert services["langfuse-web"]["environment"]["NEXTAUTH_URL"] == f"{scheme}://langfuse.localhost"
PY
echo "access mode Compose defaults: PASS (3 configurations)"

for mode in "local http dual localhost internal true" "local https dual example.test internal true" "public https https example.com acme true" "proxy https http example.com none false"; do
  read -r access scheme listen domain issuer published <<< "$mode"
  for console in off on; do
    for proxies in "" "172.30.0.0/24"; do
      if [[ "$access" == proxy && -z "$proxies" ]]; then continue; fi
      docker run --rm -e "LG_RUSTFS_CONSOLE=$console" -e "LG_TRUSTED_PROXIES=$proxies" \
        -e "LG_ACCESS_MODE=$access" -e "LG_SCHEME=$scheme" -e "LG_HTTPS_PUBLISHED=$published" \
        -e "LG_OPERATOR_ALLOW=127.0.0.0/8 ::1" -e "LG_LISTEN_SCHEME=$listen" -e "LG_PUBLIC_DOMAIN=$domain" -e "LG_TLS_ISSUER=$issuer" \
        -v "$root/docker/caddy/Caddyfile:/etc/caddy/Caddyfile:ro" \
        -v "$root/docker/caddy/access-mode.sh:/etc/caddy/access-mode.sh:ro" --entrypoint /bin/sh \
        "$(docker compose --env-file "$work/.env" config --images | grep '^caddy')" \
        /etc/caddy/access-mode.sh caddy validate --config /etc/caddy/Caddyfile >/dev/null || { echo "Caddy validation failed for $mode (console=$console, proxies=$proxies)" >&2; exit 1; }
    done
  done
done
echo "Caddyfile: PASS (14 configurations)"

mapfile -t scripts < <(git ls-files '*.sh')
shellcheck "${scripts[@]}"
echo "shellcheck: PASS"

mapfile -t py < <(git ls-files '*.py')
python3 -m py_compile "${py[@]}"
echo "python: PASS"
