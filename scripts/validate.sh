#!/usr/bin/env bash
# Static validation: what CI runs on every push. Only disposable Caddy validation containers run.
set -euo pipefail
root=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
cd "$root"

for tool in docker python3 shellcheck openssl; do
  command -v "$tool" >/dev/null || { echo "missing tool: $tool" >&2; exit 1; }
done
docker compose version >/dev/null

work=$(mktemp -d)
trap 'rm -rf "$work"' EXIT

# Validate repository defaults independently of the invoking installation.
for key in $(compgen -e); do
  case "$key" in LG_*|LANGFUSE_*|RUSTFS_*|CLICKHOUSE_*|VALKEY_*|POSTGRES_*|LITELLM_*|UI_USERNAME|UI_PASSWORD|NEXTAUTH_SECRET|SALT|COMPOSE_*) unset "$key";; esac
done
export COMPOSE_FILE="$root/compose.yaml:$root/compose.local.yaml"
export LG_BACKUP_DIR="$work/backups" LANGFUSE_INIT_USER_EMAIL=validate@gateway.test
python3 scripts/bootstrap.py --env-file "$work/.env" --render-only >/dev/null
echo "env render: PASS"

docker compose --env-file "$work/.env" config -q
docker compose --env-file "$work/.env" config --format json > "$work/config.json"
python3 - "$work/config.json" <<'PY'
import json, sys
config = json.load(open(sys.argv[1]))
assert config["services"]["litellm"].get("stop_signal") == "SIGINT", "LiteLLM requires graceful interpreter exit"
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

python3 - "$work" <<'PYIMAGES'
import json, os, pathlib, subprocess, sys
work = pathlib.Path(sys.argv[1])
command = ["docker", "compose", "--env-file", str(work / ".env"), "config", "--format", "json"]
def images(environment):
    result = subprocess.run(command, env=environment, capture_output=True, text=True)
    assert result.returncode == 0, "image override Compose resolution failed"
    return {name: service["image"] for name, service in json.loads(result.stdout)["services"].items()}
defaults = images(os.environ)
cases = {"litellm": ("LG_LITELLM_IMAGE", "localhost:5000/experiment/gateway:trial"),
         "postgres": ("LG_POSTGRES_IMAGE", "mirror.test/store@sha256:" + "a" * 64),
         "rustfs-init": ("LG_AWS_CLI_IMAGE", "local-helper:trial")}
original = (work / ".env").read_text()
with (work / ".env").open("a") as handle:
    for key, value in cases.values():
        handle.write(f"{key}={value}\n")
assert images(os.environ) == {**defaults, **{name: ref for name, (_, ref) in cases.items()}}
assert images({**os.environ, **{key: "" for key, _ in cases.values()}}) == defaults
# Leave subsequent default gates on the original settings.
(work / ".env").write_text(original)
print("image overrides: PASS (app/store/helper, complete refs and empty shell fallback)")
PYIMAGES

for mode in local public proxy; do
  export COMPOSE_FILE="$root/compose.yaml:$root/compose.$mode.yaml"
  LG_ACCESS_MODE=$mode docker compose --env-file "$work/.env" config --format json > "$work/$mode.json"
  LG_ACCESS_MODE=$mode LG_CONSOLE_URL=https://darkforge.tail694fe2.ts.net:8446 \
    LG_LITELLM_URL=https://darkforge.tail694fe2.ts.net:8443 \
    LG_LANGFUSE_URL=https://darkforge.tail694fe2.ts.net:8444 LG_S3_URL=https://darkforge.tail694fe2.ts.net:8445 LG_RUSTFS_URL=https://darkforge.tail694fe2.ts.net:8449 \
    docker compose --env-file "$work/.env" config --format json > "$work/$mode-origins.json"
done
python3 - "$work" <<'PY'
import json, pathlib, sys
for mode in ("local", "public", "proxy"):
    services = json.loads((pathlib.Path(sys.argv[1]) / f"{mode}.json").read_text())["services"]
    gateway = services["caddy"]
    ports = {port["target"] for port in gateway["ports"]}
    assert ports == ({80} if mode == "proxy" else {80, 443}), (mode, ports)
    assert all(port["host_ip"] == gateway["environment"]["LG_BIND_HOST"] for port in gateway["ports"])
    scheme = "http" if mode == "local" else "https"
    assert gateway["environment"]["LG_SCHEME"] == scheme
    assert services["litellm"]["environment"]["PROXY_BASE_URL"] == f"{scheme}://litellm.localhost"
    configured = json.loads((pathlib.Path(sys.argv[1]) / f"{mode}-origins.json").read_text())["services"]
    for current, langfuse, s3, litellm in (
        (services, f"{scheme}://langfuse.localhost", f"{scheme}://s3.localhost", f"{scheme}://litellm.localhost"),
        (configured, "https://darkforge.tail694fe2.ts.net:8444", "https://darkforge.tail694fe2.ts.net:8445",
         "https://darkforge.tail694fe2.ts.net:8443"),
    ):
        assert current["litellm"]["environment"]["PROXY_BASE_URL"] == litellm
        assert current["litellm"]["environment"]["LANGFUSE_HOST"] == "http://langfuse-web:3000"
        for service in ("langfuse-web", "langfuse-worker"):
            env = current[service]["environment"]
            assert env["NEXTAUTH_URL"] == langfuse
            assert env["LANGFUSE_S3_MEDIA_UPLOAD_ENDPOINT"] == s3
            assert env["LANGFUSE_S3_BATCH_EXPORT_EXTERNAL_ENDPOINT"] == s3
            for setting in ("EVENT_UPLOAD_ENDPOINT", "MEDIA_UPLOAD_INTERNAL_ENDPOINT", "BATCH_EXPORT_ENDPOINT"):
                assert env[f"LANGFUSE_S3_{setting}"] == "http://rustfs:9000"
        assert current["rustfs"]["environment"]["RUSTFS_CORS_ALLOWED_ORIGINS"] == langfuse
    for service in services:
        assert services[service].get("volumes") == configured[service].get("volumes")
    for app, port in (("CONSOLE", 8446), ("LITELLM", 8443), ("LANGFUSE", 8444), ("S3", 8445)):
        assert configured["caddy"]["environment"][f"LG_{app}_URL"] == f"https://darkforge.tail694fe2.ts.net:{port}"
PY
echo "access mode Compose origins: PASS (6 configurations)"

# Throwaway certificate inputs for the files issuer and the private ACME trust file.
mkdir "$work/certs"
openssl req -x509 -newkey ec -pkeyopt ec_paramgen_curve:P-256 -noenc -keyout "$work/certs/tls.key" -out "$work/certs/tls.crt" \
  -subj /CN=example.com -addext 'subjectAltName=DNS:example.com,DNS:*.example.com,DNS:localhost,DNS:*.localhost' -days 2 2>/dev/null
cp "$work/certs/tls.crt" "$work/acme-ca-root.crt"
export COMPOSE_FILE="$root/compose.yaml:$root/compose.public.yaml:$root/compose.acme-ca-root.yaml:$root/compose.acme-eab.yaml"
LG_ACCESS_MODE=public LG_ACME_CA_ROOT="$work/acme-ca-root.crt" LG_ACME_EAB_KEY_ID=key-id LG_ACME_EAB_HMAC=bWFj \
  docker compose --env-file "$work/.env" config --format json > "$work/acme.json"
export COMPOSE_FILE="$root/compose.yaml:$root/compose.local.yaml:$root/compose.files.yaml"
LG_TLS_ISSUER=files LG_TLS_DIR="$work/certs" docker compose --env-file "$work/.env" config --format json > "$work/files.json"
python3 - "$work" <<'PY'
import json, pathlib, sys
work = pathlib.Path(sys.argv[1])
caddy = {name: json.loads((work / f"{name}.json").read_text())["services"]["caddy"]
         for name in ("local", "public", "proxy", "acme", "files")}
assert [caddy[mode]["environment"]["LG_TLS_ISSUER"] for mode in ("local", "public", "proxy")] == ["internal", "acme", ""]
environment = caddy["acme"]["environment"]
assert (environment["LG_TLS_ISSUER"], environment["LG_ACME_TRUST"], environment["LG_ACME_ACCOUNT"]) == ("acme", "file", "eab")
assert (environment["LG_ACME_EAB_KEY_ID"], environment["LG_ACME_EAB_HMAC"]) == ("key-id", "bWFj")
root = {mount["target"]: mount for mount in caddy["acme"]["volumes"]}["/certs/acme-ca-root.crt"]
assert root["source"] == str(work / "acme-ca-root.crt") and root["read_only"], root
certs = {mount["target"]: mount for mount in caddy["files"]["volumes"]}["/certs"]
assert certs["source"] == str(work / "certs") and certs["read_only"], certs
assert caddy["files"]["environment"]["LG_TLS_ISSUER"] == "files"
# Some Compose releases drop a false create_host_path from the rendered config; check the source.
for overlay in ("compose.files.yaml", "compose.acme-ca-root.yaml"):
    assert "create_host_path: false" in open(overlay).read(), overlay + " must not create the host path"
# Caddy applies snippet defaults only to unset variables; the base must not define these.
for name in ("local", "public", "proxy", "files"):
    leaked = {"LG_ACME_TRUST", "LG_ACME_ACCOUNT", "LG_ACME_EAB_KEY_ID", "LG_ACME_EAB_HMAC"} & set(caddy[name]["environment"])
    assert not leaked, f"{name}: ACME account and trust settings leak into the base: {sorted(leaked)}"
PY
echo "TLS overlays: default issuer per mode, read-only mounts without host path creation, ACME discriminators only from overlays: PASS"
export COMPOSE_FILE="$root/compose.yaml:$root/compose.local.yaml"

acme_ca=https://ca.example.com/acme/acme/directory
# access scheme listen domain issuer published; the issuer column names a variant below, - means none.
for mode in "local http dual localhost internal true" "local http dual localhost files true" "local https dual example.test internal true" \
    "public https https example.com acme true" "public https https example.com acme-ca true" "public https https example.com acme-eab true" \
    "public https https example.com files true" "proxy https http example.com - false" "proxy https http gateway.test - false"; do
  read -r access scheme listen domain issuer published <<< "$mode"
  case "$issuer" in
    internal) tls=(-e LG_TLS_ISSUER=internal) ;;
    files) tls=(-e LG_TLS_ISSUER=files -v "$work/certs:/certs:ro") ;;
    acme) tls=(-e LG_TLS_ISSUER=acme -e LG_ACME_EMAIL= -e LG_ACME_CA=) ;;
    acme-ca) tls=(-e LG_TLS_ISSUER=acme -e LG_ACME_EMAIL=ops@example.com -e "LG_ACME_CA=$acme_ca" -e LG_ACME_TRUST=file
      -v "$work/acme-ca-root.crt:/certs/acme-ca-root.crt:ro") ;;
    acme-eab) tls=(-e LG_TLS_ISSUER=acme -e LG_ACME_EMAIL= -e "LG_ACME_CA=$acme_ca" -e LG_ACME_ACCOUNT=eab
      -e LG_ACME_EAB_KEY_ID=key-id -e LG_ACME_EAB_HMAC=bWFj) ;;
    *) tls=(-e LG_TLS_ISSUER=) ;;
  esac
  for console in off on; do
    for proxies in "" "172.30.0.0/24"; do
      if [[ "$access" == proxy && -z "$proxies" ]]; then continue; fi
      origins=()
      if [[ "$domain" == gateway.test ]]; then
        origins=(-e LG_CONSOLE_URL=https://darkforge.tail694fe2.ts.net:8446
          -e LG_LITELLM_URL=https://darkforge.tail694fe2.ts.net:8443
          -e LG_LANGFUSE_URL=https://darkforge.tail694fe2.ts.net:8444
          -e LG_S3_URL=https://darkforge.tail694fe2.ts.net:8445 -e LG_RUSTFS_URL=https://darkforge.tail694fe2.ts.net:8449)
      fi
      docker run --rm "${origins[@]}" -e "LG_RUSTFS_CONSOLE=$console" -e "LG_TRUSTED_PROXIES=$proxies" \
        -e "LG_ACCESS_MODE=$access" -e "LG_SCHEME=$scheme" -e "LG_HTTPS_PUBLISHED=$published" \
        -e "LG_OPERATOR_ALLOW=127.0.0.0/8 ::1" -e "LG_LISTEN_SCHEME=$listen" -e "LG_PUBLIC_DOMAIN=$domain" "${tls[@]}" \
        -v "$root/docker/caddy/Caddyfile:/etc/caddy/Caddyfile:ro" \
        -v "$root/docker/caddy/access-mode.sh:/etc/caddy/access-mode.sh:ro" --entrypoint /bin/sh \
        "$(docker compose --env-file "$work/.env" config --images | grep '^caddy')" \
        /etc/caddy/access-mode.sh caddy validate --config /etc/caddy/Caddyfile >/dev/null || { echo "Caddy validation failed for $mode (console=$console, proxies=$proxies)" >&2; exit 1; }
    done
  done
done
echo "Caddyfile: PASS (32 configurations: local-internal, local-files, public-acme, public-acme-ca, public-acme-eab, public-files, proxy)"

# The canonical contract lives in platform-edge; CI has no sibling checkout to compare with.
sync="${PLATFORM_EDGE_DIR:-$root/../platform-edge}/scripts/sync-conventions.sh"
if [[ -x "$sync" || -n "${PLATFORM_EDGE_DIR:-}" ]]; then
  "$sync" --check . >/dev/null
  echo "conventions: PASS (matches platform-edge)"
else
  echo "conventions: SKIP (no platform-edge checkout at ${sync%/scripts/*}; set PLATFORM_EDGE_DIR)"
fi

mapfile -t scripts < <(git ls-files '*.sh')
shellcheck "${scripts[@]}"
echo "shellcheck: PASS"

mapfile -t py < <(git ls-files '*.py')
python3 -m py_compile "${py[@]}"
echo "python: PASS"
