#!/usr/bin/env python3
"""Bring the LLM Gateway Stack up from a clean checkout, or refuse with a reason.

Same contract as the backplane's infra/bootstrap/prepare.ts: lock the env file, fill in
missing secrets, refuse to invent secrets over existing data, create the shared platform
network, start the stack, wait for readiness, print the next step. Exit codes: 0 ready,
1 refused (the JSON line on stderr names why), 2 bad usage, 3 the stack did not become
ready. Python 3.11+ standard library only.
"""

from __future__ import annotations

import argparse
import fcntl
import http.client
import ipaddress
import json
import os
import re
import secrets
import shutil
import socket
import ssl
import subprocess
import sys
import time
import urllib.parse
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

PROJECT = "llm-gateway-stack"
NETWORK = "platform"
# Platform Network allocation shared by every stack (docs/conventions.md). Edge's reserved
# address lies outside the dynamic range, so siblings can trust it without discovery.
PLATFORM_SUBNET = "172.30.0.0/24"
PLATFORM_IP_RANGE = "172.30.0.128/25"
EDGE_PROXY = "172.30.0.2/32"
VOLUMES = ("clickhouse-data", "clickhouse-logs", "rustfs-data", "valkey-data", "caddy-data", "caddy-config")

# Secrets the stack needs and how many random bytes each gets (hex encoded).
SECRETS: dict[str, int] = {
    "LITELLM_MASTER_KEY": 32,
    "UI_PASSWORD": 24,
    "LITELLM_SALT_KEY": 24,
    "POSTGRES_PASSWORD": 24,
    "LG_POSTGRES_EXPORTER_PASSWORD": 24,
    "LITELLM_DB_PASSWORD": 24,
    "LANGFUSE_DB_PASSWORD": 24,
    "NEXTAUTH_SECRET": 32,
    "LANGFUSE_SALT": 24,
    "LANGFUSE_ENCRYPTION_KEY": 32,
    "CLICKHOUSE_PASSWORD": 24,
    "RUSTFS_ACCESS_KEY": 10,
    "RUSTFS_SECRET_KEY": 24,
    "VALKEY_PASSWORD": 24,
    "LANGFUSE_INIT_USER_PASSWORD": 16,
}
# Langfuse project keys keep the upstream prefixes so clients recognise them.
PREFIXED: dict[str, tuple[str, int]] = {
    "LANGFUSE_INIT_PROJECT_PUBLIC_KEY": ("pk-lf-", 16),
    "LANGFUSE_INIT_PROJECT_SECRET_KEY": ("sk-lf-", 24),
}
# Settings with a default the template already carries; bootstrap never rewrites them.
MANAGED = set(SECRETS) | set(PREFIXED) | {"UI_USERNAME"}
# Compose project names of earlier generations whose data must not be silently reused.
LEGACY_PROJECTS = ("llm-gateway", "litellm-langfuse")
ENV_LINE = re.compile(r"^(?:export\s+)?(?P<key>[A-Z][A-Z0-9_]*)=(?P<value>.*)$")
# Status v2 components (docs/conventions.md): the contract's stable ID, which is also the
# Compose service, then display name, kind and the setting holding its browser or API origin.
COMPONENTS = (
    ("caddy", "Caddy", "gateway", None),
    ("litellm", "LiteLLM", "app", "LG_LITELLM_URL"),
    ("langfuse-web", "Langfuse", "app", "LG_LANGFUSE_URL"),
    ("langfuse-worker", "Langfuse worker", "app", None),
    ("postgres", "PostgreSQL", "datastore", None),
    ("clickhouse", "ClickHouse", "datastore", None),
    ("valkey", "Valkey", "datastore", None),
    ("rustfs", "RustFS", "datastore", "LG_S3_URL"),
    ("postgres-exporter", "PostgreSQL exporter", "collector", None),
    ("valkey-exporter", "Valkey exporter", "collector", None),
)
# Every component ships dotted numeric release tags, some with a `v` or a pre-release suffix.
RELEASE_TAG = r"v?[0-9]+(?:\.[0-9]+)+(?:-[A-Za-z0-9.]+)?"


class Refused(Exception):
    def __init__(self, code: str, detail: str = "") -> None:
        super().__init__(code)
        self.code = code
        self.detail = detail


Runner = Callable[..., subprocess.CompletedProcess[str]]


def run(argv: list[str], *, timeout=None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(argv, text=True, capture_output=True, check=False, timeout=timeout)


def read_env(path: Path) -> tuple[list[str], dict[str, str]]:
    """Return the raw lines and the managed assignments. Unmanaged lines are kept verbatim."""
    if not path.exists():
        return [], {}
    lines = path.read_text(encoding="utf-8").splitlines()
    values: dict[str, str] = {}
    for line in lines:
        match = ENV_LINE.match(line)
        if not match:
            continue
        key, value = match.group("key"), match.group("value")
        if key in MANAGED:
            if key in values:
                raise Refused("env_repair_required", f"{key} is set twice in {path}")
            if value.startswith(("'", '"')) or "${" in value:
                raise Refused("env_repair_required", f"{key} must be a plain value in {path}")
            values[key] = value
    return lines, values


def generate(missing: set[str]) -> dict[str, str]:
    fresh: dict[str, str] = {}
    for key in sorted(missing):
        if key == "UI_USERNAME":
            fresh[key] = "admin"
        elif key in SECRETS:
            fresh[key] = secrets.token_hex(SECRETS[key])
        else:
            prefix, size = PREFIXED[key]
            fresh[key] = prefix + secrets.token_hex(size)
    return fresh


def write_env(path: Path, lines: list[str], template: Path, fresh: dict[str, str]) -> None:
    if not lines:
        lines = template.read_text(encoding="utf-8").splitlines()
        lines += ["", "# Generated by scripts/bootstrap.py. Keep this file private and backed up."]
    lines += [f"{key}={value}" for key, value in fresh.items()]
    tmp = path.with_name(path.name + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write("\n".join(lines) + "\n")
    os.replace(tmp, path)
    os.chmod(path, 0o600)


def unquote(value: str) -> str:
    """Compose accepts 'x' and "x"; a bare value is used as is."""
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
        return value[1:-1]
    return value


def backup_policy(settings: dict[str, str]) -> bool:
    """Storage policy follows Compose precedence, including an explicitly empty shell value."""
    value = os.environ.get("LG_ALLOW_SAME_FILESYSTEM_BACKUP",
                           settings.get("LG_ALLOW_SAME_FILESYSTEM_BACKUP", "false"))
    if value not in ("true", "false"):
        raise Refused("invalid_backup_policy", "LG_ALLOW_SAME_FILESYSTEM_BACKUP must be true or false")
    if value == "true":
        print("WARNING: development same-filesystem backups enabled; disk loss affects both "
              "live data and backups. This does not meet the production backup contract.", file=sys.stderr)
    return value == "true"


def check_backup_storage(backups: Path, data: Path, allow_same_filesystem: bool = False) -> None:
    backups, data = backups.resolve(), data.resolve()
    if not backups.is_dir():
        raise Refused("backup_dir_missing", "LG_BACKUP_DIR must exist and its storage must be mounted")
    if backups.is_relative_to(data) or data.is_relative_to(backups):
        raise Refused("backup_dir_overlap", "backup and Postgres data paths must not overlap")
    # Inspect the nearest existing parent before creating the cluster directory.
    parent = data
    while not parent.exists():
        parent = parent.parent
    if backups.stat().st_dev == parent.stat().st_dev and not allow_same_filesystem:
        raise Refused("backup_dir_same_filesystem", "backup and Postgres data must use different filesystems")


def project_name(settings: dict[str, str]) -> str:
    """Same precedence as Compose: shell environment, then the env file, then name:."""
    return os.environ.get("COMPOSE_PROJECT_NAME") or unquote(settings.get("COMPOSE_PROJECT_NAME", "")) or PROJECT


def installation_state(root: Path, data_dir: Path, runner: Runner, project: str = PROJECT,
                       prefix: str = PROJECT) -> list[str]:
    """Every place an earlier installation of this project could have left data."""
    found: list[str] = []
    prefixes = tuple(f"{name}_" for name in (project, *LEGACY_PROJECTS))
    if data_dir.exists():
        if not os.access(data_dir, os.R_OK | os.X_OK):
            raise Refused("postgres_dir_unreadable", str(data_dir))
        if any(data_dir.iterdir()):
            found.append(f"postgres data at {data_dir}")
    result = runner(["docker", "volume", "ls", "--format", "{{.Name}}"])
    if result.returncode != 0:
        raise Refused("docker_unavailable", result.stderr.strip())
    for name in result.stdout.split():
        if name.startswith(prefixes) or name in volume_names(prefix):
            found.append(f"volume {name}")
    return found


def images(command: list[str], runner: Runner = run) -> dict[str, str]:
    """Resolve service images with the caller's Compose environment and overlays."""
    result = runner(command + ["config", "--format", "json"])
    if result.returncode:
        raise Refused("compose_config_failed", "cannot resolve image metadata; check Compose settings")
    return {name: service["image"] for name, service in json.loads(result.stdout)["services"].items()}


def utc(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def last_checkpoint(backups: Path) -> str | None:
    """Newest readable Checkpoint manifest time, or None when none can be read."""
    times = []
    try:
        paths = [path for path in backups.iterdir() if re.fullmatch(r"\d{8}T\d{12}Z", path.name)]
    except OSError:
        return None
    for path in paths:
        try:
            times.append(datetime.fromisoformat(json.loads((path / "manifest.json").read_text())["timestamp"]))
        except (OSError, ValueError, KeyError, TypeError):
            continue
    return utc(max(times)) if times else None


def status_document(available: dict[str, str], selected: dict[str, str], settings: dict[str, str],
                    backups: Path, configured_at: str) -> dict:
    """The public Status v2 document: configured images and origins, never secrets."""
    components = []
    for component, name, kind, origin in COMPONENTS:
        if component not in available:
            continue
        image = available[component].split("@", 1)[0]
        tag = image.rsplit(":", 1)[1] if ":" in image.rsplit("/", 1)[-1] else ""
        record = {"id": component, "name": name, "kind": kind, "enabled": component in selected,
                  "image": image,
                  "version": tag if re.fullmatch(RELEASE_TAG, tag) else None,
                  "health": "/health/" + component}
        if origin and settings.get(origin):
            record["url"] = settings[origin]
        components.append(record)
    return {"contract": 2, "stack": "gateway", "configuredAt": configured_at, "components": components,
            "features": {"backups": {"configured": True, "lastCheckpointAt": last_checkpoint(backups)}}}


def console_dir(root: Path) -> Path:
    """Caddy mounts this directory; Docker would create a missing one owned by root."""
    console = root / "data" / "console"
    console.mkdir(parents=True, exist_ok=True, mode=0o755)
    return console


def write_status(root: Path, document: dict) -> None:
    console = console_dir(root)
    path = console / "status.json"
    temporary = console / ".status.json.tmp"
    temporary.write_text(json.dumps(document, indent=2) + "\n", encoding="utf-8")
    # Caddy reads the mount as another user; replace the file whole so it never sees a partial one.
    os.chmod(temporary, 0o644)
    os.replace(temporary, path)


def write_versions(root: Path, compose: Path, refs: dict[str, str]) -> None:
    """Restore's version record (scripts/checkpoint.py). Caddy no longer serves it."""
    console = root / "data" / "console"
    console.mkdir(parents=True, exist_ok=True, mode=0o755)
    tags = {}
    for service, ref in refs.items():
        name, _, digest = ref.partition("@")
        tags[service] = (name.rsplit(":", 1)[-1] if ":" in name.rsplit("/", 1)[-1]
                         else digest or "latest")
    doc = {
        "pinnedAt": datetime.fromtimestamp(compose.stat().st_mtime, timezone.utc).isoformat(),
        "configuredAt": datetime.now(timezone.utc).isoformat(),
        "images": {
            "litellm": re.sub(r"^v(?=\d)", "", tags.get("litellm", "unknown")),
            "langfuse": tags.get("langfuse-web", "unknown"),
            "rustfs": tags.get("rustfs", "unknown"),
            "postgres": tags.get("postgres", "unknown"),
            "clickhouse": tags.get("clickhouse", "unknown"),
            "valkey": tags.get("valkey", "unknown"),
            "caddy": tags.get("caddy", "unknown"),
        },
    }
    (console / "versions.json").write_text(json.dumps(doc, indent=2) + "\n", encoding="utf-8")


def volume_names(prefix: str) -> list[str]:
    return [f"{prefix}-{name}" for name in VOLUMES]


def ensure_volumes(runner: Runner, prefix: str = PROJECT, project: str = PROJECT) -> None:
    result = runner(["docker", "volume", "ls", "--format", "{{.Name}}"])
    if result.returncode:
        raise Refused("docker_unavailable", result.stderr.strip())
    existing = set(result.stdout.split())
    for name in volume_names(prefix):
        if name not in existing:
            result = runner(["docker", "volume", "create", "--label",
                             "com.docker.compose.project=" + project, name])
            if result.returncode:
                raise Refused("volume_create_failed", result.stderr.strip())


def platform_allocation(settings: dict[str, str]) -> tuple[str, str]:
    subnet = settings.get("LG_PLATFORM_SUBNET") or PLATFORM_SUBNET
    ip_range = settings.get("LG_PLATFORM_IP_RANGE") or PLATFORM_IP_RANGE
    try:
        network, dynamic = ipaddress.IPv4Network(subnet), ipaddress.IPv4Network(ip_range)
    except ValueError as error:
        raise Refused("invalid_platform_network",
                      "LG_PLATFORM_SUBNET and LG_PLATFORM_IP_RANGE must be IPv4 networks") from error
    if not dynamic.subnet_of(network):
        raise Refused("invalid_platform_network", "LG_PLATFORM_IP_RANGE must lie inside LG_PLATFORM_SUBNET")
    # Docker could hand a trusted address to any container attached to the network.
    for proxy in settings.get("LG_TRUSTED_PROXIES", "").split():
        try:
            trusted = ipaddress.ip_network(proxy, strict=False)
        except ValueError:
            continue
        if trusted.version == 4 and trusted.overlaps(dynamic):
            raise Refused("invalid_platform_network",
                          f"LG_PLATFORM_IP_RANGE {dynamic} must exclude trusted proxy {proxy}")
    return str(network), str(dynamic)


def ensure_network(runner: Runner, name: str = NETWORK, subnet: str | None = None,
                   ip_range: str | None = None) -> None:
    if subnet is None or ip_range is None:
        # Checkpoint restore passes no settings; the shell may carry a disposable allocation.
        subnet, ip_range = platform_allocation(os.environ)
    inspect = ["docker", "network", "inspect", "--format", "{{json .IPAM.Config}}", name]
    probe = runner(inspect)
    if probe.returncode != 0:
        gateway = str(next(ipaddress.IPv4Network(subnet).hosts()))
        created = runner(["docker", "network", "create", "--driver", "bridge", "--subnet", subnet,
                          "--ip-range", ip_range, "--gateway", gateway, name])
        if created.returncode == 0:
            return
        # Another bootstrap may have created it first; validate that network instead.
        probe = runner(inspect)
        if probe.returncode != 0:
            raise Refused("network_create_failed", created.stderr.strip())
    try:
        configs = json.loads(probe.stdout) or []
    except ValueError:
        configs = []
    observed = [(config.get("Subnet", ""), config.get("IPRange", "")) for config in configs]
    # A second IPv4 pool would also hand out addresses; IPv6 pools are left to the operator.
    ipv4 = [entry for entry in observed if ":" not in entry[0]]
    if ipv4 != [(subnet, ip_range)]:
        found = "; ".join(f"subnet {s or 'none'} ip-range {r or 'none'}" for s, r in observed) or "no IPAM configuration"
        raise Refused("platform_network_mismatch",
                      f"network {name} has {found}; expected subnet {subnet} ip-range {ip_range}. "
                      f"One-time fix: stop every stack on {name}, run `docker network rm {name}`, "
                      "then rerun bootstrap")


def compose_up(root: Path, env_file: Path, runner: Runner, langfuse_origin: str) -> None:
    result = runner([
        "env", "LG_LANGFUSE_URL=" + langfuse_origin,
        "docker", "compose", "--project-directory", str(root), "--env-file", str(env_file),
        "up", "--detach", "--wait", "--wait-timeout", "300",
    ])
    if result.returncode != 0:
        raise Refused("compose_up_failed", (result.stderr or result.stdout).strip()[-2000:])


def access_settings(settings: dict[str, str]) -> dict[str, str]:
    values = dict(settings)
    mode = values.get("LG_ACCESS_MODE") or "local"
    defaults = {
        "local": ("http", "dual", "internal"),
        "public": ("https", "https", "acme"),
        "proxy": ("https", "http", "none"),
    }
    if mode not in defaults:
        raise Refused("invalid_access_mode", "LG_ACCESS_MODE must be local, public or proxy")
    values["LG_ACCESS_MODE"] = mode
    # Compose renders the same default for an empty value.
    values["LG_TRUSTED_PROXIES"] = values.get("LG_TRUSTED_PROXIES") or EDGE_PROXY
    values["LG_SCHEME"] = values.get("LG_SCHEME") or defaults[mode][0]
    values["LG_LISTEN_SCHEME"], values["LG_TLS_ISSUER"] = defaults[mode][1:]
    values.setdefault("LG_PUBLIC_DOMAIN", "localhost")
    if not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9.-]*", values["LG_PUBLIC_DOMAIN"]):
        raise Refused("invalid_access_settings", "LG_PUBLIC_DOMAIN must be a DNS hostname")
    if re.fullmatch(r"[0-9.]+", values["LG_PUBLIC_DOMAIN"]):
        raise Refused("invalid_access_settings", "use a DNS application domain; 127.0.0.1 is a Local Mode root alias")
    suffix = values.get("LG_PUBLIC_PORT_SUFFIX", "")
    if not re.fullmatch(r"(?::[0-9]+)?", suffix):
        raise Refused("invalid_access_settings", "LG_PUBLIC_PORT_SUFFIX must be empty or :port")
    port = suffix[1:].lstrip("0")
    if suffix and (not port or len(port) > 5 or int(port) > 65535):
        raise Refused("invalid_access_settings", "LG_PUBLIC_PORT_SUFFIX must use a port from 1 to 65535")
    bind = values.get("LG_BIND_HOST") or "127.0.0.1"
    # Docker also unmaps IPv4-mapped unspecified addresses to the IPv4 wildcard.
    mapped_wildcard = re.fullmatch(
        r"\[?(?:[0:]*::[0:]*ffff:(?:0+:0+|0\.0\.0\.0)|"
        r"(?:0+:){5}ffff:(?:0+:0+|0\.0\.0\.0|:|:0+|0+::))\]?", bind, re.IGNORECASE)
    if mode == "proxy" and (bind == "0.0.0.0" or re.fullmatch(r"\[?[0:.]*:[0:.]*\]?", bind)
                            or mapped_wildcard):
        raise Refused("invalid_access_settings", "proxy requires a loopback or specific-interface LG_BIND_HOST; "
                      "see docs/operations/ingress.md")
    if (values["LG_SCHEME"] not in ("http", "https")
            or (mode == "public" and values["LG_SCHEME"] != "https")
            or (mode == "proxy" and not values.get("LG_TRUSTED_PROXIES", "").strip())
            or (mode == "public" and (values["LG_PUBLIC_DOMAIN"] == "localhost"
                                      or values["LG_PUBLIC_DOMAIN"].endswith(".localhost")))):
        raise Refused("invalid_access_settings", "invalid public origin or missing proxy trust; "
                      "see docs/operations/ingress.md")
    files = values.get("COMPOSE_FILE", "compose.yaml:compose.${LG_ACCESS_MODE:-local}.yaml")
    files = files.replace("${LG_ACCESS_MODE:-local}", mode).split(os.pathsep)
    if mode != "local" and not any(Path(name).name == f"compose.{mode}.yaml" for name in files):
        raise Refused("invalid_access_settings", f"COMPOSE_FILE must include compose.{mode}.yaml")
    access_keys = {"LG_ACCESS_MODE", "LG_BIND_HOST", "LG_SCHEME", "LG_PUBLIC_DOMAIN", "LG_PUBLIC_PORT_SUFFIX",
                   "LG_TRUSTED_PROXIES", "LG_LISTEN_SCHEME", "LG_TLS_ISSUER"}
    access_keys.update("LG_" + app + "_URL" for app in ("CONSOLE", "LITELLM", "LANGFUSE", "S3", "RUSTFS", "GRAFANA", "BACKPLANE"))
    environment = {key: value for key, value in values.items() if key in access_keys}
    environment["LG_HTTPS_PUBLISHED"] = str(mode != "proxy").lower()
    origins = subprocess.run(
        ["sh", str(Path(__file__).resolve().parent.parent / "docker/caddy/access-mode.sh"), "--origins"],
        env=environment, capture_output=True, text=True,
    )
    if origins.returncode:
        raise Refused("invalid_access_settings", origins.stderr.strip())
    values.update(json.loads(origins.stdout))
    return values


class LocalHTTPSConnection(http.client.HTTPSConnection):
    """Connect to the local listener while verifying the public TLS hostname."""

    def __init__(self, hostname, port, address, context):
        super().__init__(hostname, port, timeout=5, context=context)
        self.address = address
        self.context = context

    def connect(self):
        sock = socket.create_connection((self.address, self.port), self.timeout)
        try:
            self.sock = self.context.wrap_socket(sock, server_hostname=self.host)
        except BaseException:
            sock.close()
            raise


def wait_ready(url: str, timeout: float = 120.0, host: str | None = None,
               context: ssl.SSLContext | None = None) -> None:
    parsed = urllib.parse.urlsplit(url)
    deadline = time.monotonic() + timeout
    last = ""
    while time.monotonic() < deadline:
        connection = (LocalHTTPSConnection(host or parsed.hostname, parsed.port,
                                           parsed.hostname, context or ssl.create_default_context())
                      if parsed.scheme == "https" else
                      http.client.HTTPConnection(parsed.hostname, parsed.port, timeout=5))
        try:
            connection.request("GET", parsed.path, headers={"Host": host} if host else {})
            response = connection.getresponse()
            if response.status == 200:
                return
            last = f"http {response.status}"
        except (OSError, http.client.HTTPException) as error:
            last = str(error)
        finally:
            connection.close()
        time.sleep(3)
    raise Refused("not_ready", f"{url}: {last}")


def probe_gateway(settings, command, runner):
    settings = access_settings(settings)
    domain = settings.get("LG_PUBLIC_DOMAIN", "localhost")
    bind = settings.get("LG_BIND_HOST", "127.0.0.1")
    bind = bind[1:-1] if bind.startswith("[") and bind.endswith("]") else bind
    address = "127.0.0.1" if bind in ("0.0.0.0", "127.0.0.1", "") else bind
    if address == "::":
        address = "::1"
    schemes = ("http", "https") if settings["LG_ACCESS_MODE"] == "local" else (settings["LG_LISTEN_SCHEME"],)
    for scheme in schemes:
        port = settings.get("LG_HTTPS_PORT" if scheme == "https" else "LG_HTTP_PORT") or ("443" if scheme == "https" else "80")
        context = None
        if scheme == "https":
            context = ssl.create_default_context()
            if settings["LG_TLS_ISSUER"] == "internal":
                result = runner(command + ["exec", "-T", "caddy", "cat",
                                           "/data/caddy/pki/authorities/local/root.crt"])
                if result.returncode:
                    raise Refused("internal_ca_unavailable", "cannot read Caddy's internal root certificate")
                context.load_verify_locations(cadata=result.stdout)
        local = f"{scheme}://{'[' + address + ']' if ':' in address else address}:{port}"
        for path in ("/health/litellm", "/health/langfuse"):
            wait_ready(local + path, host=domain, context=context)


def bootstrap(argv: list[str], runner: Runner = run) -> int:
    parser = argparse.ArgumentParser(prog="bootstrap.py", description=__doc__.splitlines()[0])
    parser.add_argument("--env-file", default=".env")
    parser.add_argument("--template", default=".env.example")
    parser.add_argument("--render-only", action="store_true",
                        help="write the env file, start nothing")
    args = parser.parse_args(argv)
    root = Path(__file__).resolve().parent.parent
    env_file = (root / args.env_file).resolve()
    template = (root / args.template).resolve()

    if shutil.which("docker") is None and not args.render_only:
        raise Refused("docker_missing", "install Docker with the Compose plugin")

    lock_path = env_file.with_name(env_file.name + ".lock")
    with open(lock_path, "w", encoding="utf-8") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise Refused("bootstrap_already_running", str(lock_path)) from error

        lines, present = read_env(env_file)
        # Compose lets the shell override the env file. A shell value that differs from
        # the saved one would run the stack with a secret the file does not record.
        conflicts = sorted(k for k in MANAGED if k in present and k in os.environ and os.environ[k] != present[k])
        if conflicts:
            raise Refused("shell_env_conflict", "unset in the shell or fix .env: " + ", ".join(conflicts))
        missing = MANAGED - set(present)
        settings = {
            m.group("key"): unquote(m.group("value"))
            for m in (ENV_LINE.match(line) for line in (lines or template.read_text().splitlines())) if m
        }
        # Match Compose's shell precedence for operator settings as well as secrets.
        settings.update({key: value for key, value in os.environ.items()
                         if key in settings or key.startswith("LG_") or key == "COMPOSE_FILE"})
        requested_langfuse = settings.get("LG_LANGFUSE_URL")
        settings = access_settings(settings)
        allocation = platform_allocation(settings)
        raw_langfuse = requested_langfuse or (settings["LG_SCHEME"] + "://langfuse."
                                             + settings["LG_PUBLIC_DOMAIN"]
                                             + settings.get("LG_PUBLIC_PORT_SUFFIX", ""))
        allow_same_filesystem = backup_policy(settings)
        project = project_name(settings)
        prefix = os.environ.get("LG_VOLUME_PREFIX", settings.get("LG_VOLUME_PREFIX")) or PROJECT
        data_dir_setting = os.environ.get("LG_POSTGRES_DATA_DIR") or settings.get("LG_POSTGRES_DATA_DIR", "./data/postgres")
        data_dir = Path(data_dir_setting) if os.path.isabs(data_dir_setting) else (root / data_dir_setting).resolve()
        installation = env_file == (root / ".env").resolve()

        # --render-only on a scratch env file (validate.sh, tests) touches nothing but that
        # file. On the installation's own .env the guard still applies: no new secrets
        # over existing data, in any mode.
        if missing and (installation or not args.render_only):
            existing = installation_state(root, data_dir, runner, project, prefix)
            if existing:
                raise Refused(
                    "existing_installation_missing_secrets",
                    "restore the original .env before starting; found " + "; ".join(existing),
                )
        if missing:
            fresh = generate(missing)
            # A secret supplied in the shell on a fresh install is the operator's choice;
            # record it instead of generating a different one.
            fresh.update({k: os.environ[k] for k in missing if os.environ.get(k)})
            write_env(env_file, lines, template, fresh)
        # RustFS compares CORS origins literally. Preserve existing lines and append
        # a canonical override only when normalization or a shell override needs it.
        canonical_langfuse = settings["LG_LANGFUSE_URL"]
        if raw_langfuse != canonical_langfuse or os.environ.get("LG_LANGFUSE_URL"):
            saved_lines, _ = read_env(env_file)
            saved_langfuse = next((unquote(m.group("value")) for line in reversed(saved_lines)
                                   if (m := ENV_LINE.match(line))
                                   and m.group("key") == "LG_LANGFUSE_URL"), "")
            if saved_langfuse != canonical_langfuse:
                write_env(env_file, saved_lines, template, {"LG_LANGFUSE_URL": canonical_langfuse})
        if args.render_only:
            print(json.dumps({"env": str(env_file), "project": project, "generated": sorted(missing)}))
            return 0

        email = settings.get("LANGFUSE_INIT_USER_EMAIL", "").strip()
        if not email or email.lower().endswith("@example.com"):
            raise Refused("langfuse_login_required", "set LANGFUSE_INIT_USER_EMAIL to your login email")
        backup_setting = os.environ.get("LG_BACKUP_DIR", settings.get("LG_BACKUP_DIR", ""))
        if not backup_setting:
            raise Refused("backup_dir_required", "set LG_BACKUP_DIR to a separate mounted filesystem")
        backup_dir = (root / backup_setting).resolve()
        check_backup_storage(backup_dir, data_dir, allow_same_filesystem)

        configured_at = utc(datetime.now(timezone.utc))
        # 0755: the postgres user must traverse this directory to reach its cluster,
        # which the image creates underneath as 18/docker with mode 0700.
        data_dir.mkdir(parents=True, exist_ok=True, mode=0o755)
        os.chmod(data_dir, 0o755)
        console_dir(root)
        command = ["env", "LG_LANGFUSE_URL=" + canonical_langfuse, "docker", "compose",
                   "--project-directory", str(root), "--env-file", str(env_file)]
        # Every service, then those the selected profiles enable.
        document = status_document(images(command + ["--profile", "*"], runner), images(command, runner),
                                   settings, backup_dir, configured_at)

        ensure_network(runner, settings.get("LG_PLATFORM_NETWORK", NETWORK), *allocation)
        ensure_volumes(runner, prefix, project)
        compose_up(root, env_file, runner, canonical_langfuse)
        probe_gateway(settings, ["docker", "compose", "--project-directory", str(root),
                                 "--env-file", str(env_file)], runner)
        write_status(root, document)
        print(json.dumps({
            "console": settings["LG_CONSOLE_URL"] + "/",
            "litellm": settings["LG_LITELLM_URL"] + "/",
            "langfuse": settings["LG_LANGFUSE_URL"] + "/",
            "langfuseLogin": email,
            "next": "Log in to Langfuse with the email above and LANGFUSE_INIT_USER_PASSWORD from .env; "
                    "call the API with LITELLM_MASTER_KEY or create a virtual key in LiteLLM.",
        }))
        return 0


def main() -> int:
    try:
        return bootstrap(sys.argv[1:])
    except Refused as refused:
        print(json.dumps({"error": refused.code, "detail": refused.detail}), file=sys.stderr)
        return 3 if refused.code in ("not_ready", "compose_up_failed") else 1
    except SystemExit as exit_:  # argparse
        return 2 if exit_.code not in (0, None) else 0


if __name__ == "__main__":
    raise SystemExit(main())
