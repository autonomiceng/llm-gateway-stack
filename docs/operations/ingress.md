# Ingress and access modes

How the stack is reached: hostnames, the three access modes, certificates, Tailscale, the
shared host behind Platform Edge, and what the gateway exposes to scrapers.

- [Hostnames and modes](#hostnames-and-modes)
- [Local Mode (default)](#local-mode-default)
- [Public Mode](#public-mode)
- [Tailscale](#tailscale)
- [Corporate certificates and private ACME](#corporate-certificates-and-private-acme)
- [Shared host](#shared-host)
- [Application URLs](#application-urls)
- [Operator access and health](#operator-access-and-health)
- [Metrics](#metrics)
- [RustFS admin console](#rustfs-admin-console)
- [What is never published](#what-is-never-published)
- [Troubleshooting](#troubleshooting)

## Hostnames and modes

Caddy is the only published entry. Applications use these hostnames under one domain
(`LG_PUBLIC_DOMAIN`, default `localhost`):

| Hostname | Upstream |
| --- | --- |
| `<domain>` | Stack Console and `/health/*` probes |
| `litellm.<domain>` | LiteLLM API and admin UI (`/ui/`, LiteLLM's own login) |
| `langfuse.<domain>` | Langfuse |
| `s3.<domain>` | RustFS S3 API, for presigned media and export URLs |
| `rustfs.<domain>` | RustFS admin console (RustFS's own login) |

`LG_ACCESS_MODE` selects the listeners and the default certificate issuer:

| Mode | HTTP | HTTPS | Issuer (`LG_TLS_ISSUER`) |
| --- | --- | --- | --- |
| Local (`local`) | Served without redirects | Served on loopback | `internal` (default) or `files` |
| Public (`public`) | Redirects to HTTPS, except root `/health/*` | Served for your domain | `acme` (default; Let's Encrypt or `LG_ACME_CA`) or `files` |
| Proxy (`proxy`) | From the other gateway | Handled by the other gateway | Unused |

Bootstrap records `COMPOSE_FILE` in `.env` as a literal list: `compose.yaml`, then
`compose.public.yaml` or `compose.proxy.yaml` for those modes (Local Mode needs no mode
file), then the issuer's TLS overlays, then any files you added. Compose 2.24.4 or newer
is required. After changing `LG_ACCESS_MODE`, run `python3 scripts/bootstrap.py`; it
records the new list and recreates services. Explicit `-f` overrides must include the
matching mode file.

For a lasting deployment choice, edit `.env`. Shell overrides apply only to that command;
bootstrap does not save them into existing settings, except that an exported
`LG_ACCESS_MODE` is saved together with the `COMPOSE_FILE` it selects. Use the same
overrides for backup and restore, or save them in `.env` first.

## Local Mode (default)

`LG_ACCESS_MODE=local` serves HTTP and self-signed HTTPS on `LG_BIND_HOST=127.0.0.1`.
Both protocols work at the same time without HTTP redirects or HSTS, including HSTS from
upstream applications. Application URLs use HTTP by default. Browsers and
systemd-resolved hosts resolve `*.localhost`; elsewhere configure DNS or use curl
`--resolve`. Nothing listens outside the host by default.

The root console accepts `localhost`, `127.0.0.1`, and arbitrary HTTP Host values.
Applications still require the explicit names in the table. Console links come from
configured origins, including when the root is opened by IP; Langfuse authentication,
S3 signatures and CORS do not acquire arbitrary aliases.

If another service owns port 80 or 443, set `LG_HTTP_PORT` and `LG_HTTPS_PORT`, and set
`LG_PUBLIC_PORT_SUFFIX` to the application's browser-facing port (for example `:8080`) so
Langfuse's login URL, presigned S3 URLs and CORS carry it. URLs then look like
`http://litellm.localhost:8080`. Optional `LG_SCHEME=https` selects HTTPS as the configured
application URL and needs the HTTPS port suffix instead; it does not disable HTTP.

Local Mode issues and renews certificates from Caddy's own CA in the existing
`caddy-data` volume. To use local HTTPS without warnings, export only its public root
certificate and install it on the clients that need this stack:

```sh
docker compose cp caddy:/data/caddy/pki/authorities/local/root.crt ./gateway-root.crt
```

Bootstrap and restore read this root from their own Caddy container for local HTTPS
health probes and verify the configured public domain. Never disable certificate
verification in clients instead. No startup command changes host trust. Never copy CA
private keys or certificate volumes between stacks; select `proxy` when Platform Edge
handles HTTPS.

## Public Mode

Set DNS A or AAAA records for the five hostnames (root, `litellm.`, `langfuse.`, `s3.` and
`rustfs.` under `LG_PUBLIC_DOMAIN`) to this host, open ports 80 and 443, and in `.env`:

```sh
LG_PUBLIC_DOMAIN=gateway.example.com
LG_ACCESS_MODE=public
LG_BIND_HOST=0.0.0.0
```

Then `python3 scripts/bootstrap.py`. Leave `LG_SCHEME` empty for the HTTPS default;
Public Mode requires HTTPS application URLs. Caddy proves ownership of your domain through
port 80, stores certificates in `caddy-data`, and redirects HTTP to the configured HTTPS
origins. Root `/health/*` probes remain available over HTTP. Redirects for unknown HTTP
hosts use the configured root domain. Application login, presigned URLs and CORS use the
same configured scheme, domain and port.

## Tailscale

Private access from your own devices without exposing anything to the internet. On a
shared host, Platform Edge runs one Tailscale node per hostname; see its
[Tailscale setup](https://github.com/autonomiceng/platform-edge/blob/main/docs/operations/tailscale.md)
and the [shared host](#shared-host) section below. Standalone, the host's own Tailscale
daemon serves each application on its own HTTPS port of the machine's tailnet name and
forwards to the stack's loopback HTTP port.

Prerequisites: Tailscale installed and logged in on the host, MagicDNS and HTTPS
certificates enabled for the tailnet (`https://login.tailscale.com/admin/dns`). Proxy
Mode has two requirements of its own: `LANGFUSE_INIT_USER_EMAIL` set on a fresh install
(bootstrap refuses with `langfuse_login_required`), and `LG_BACKUP_DIR` on a filesystem
separate from Postgres data, or `LG_ALLOW_SAME_FILESYSTEM_BACKUP=true` to accept the
shared disk (bootstrap refuses with `backup_dir_same_filesystem`); see [backup](backup.md#storage).

1. In `.env`, select Proxy Mode on a loopback port and give every application its full
   browser origin. The hostname is the machine's tailnet name; the ports are yours to
   choose and must differ per application:

   ```dotenv
   LG_ACCESS_MODE=proxy
   LG_HTTP_PORT=18080
   LG_CONSOLE_URL=https://gateway.tail-example.ts.net:8446
   LG_LITELLM_URL=https://gateway.tail-example.ts.net:8443
   LG_LANGFUSE_URL=https://gateway.tail-example.ts.net:8444
   LG_S3_URL=https://gateway.tail-example.ts.net:8445
   LG_RUSTFS_URL=https://gateway.tail-example.ts.net:8449
   ```

2. Run `python3 scripts/bootstrap.py`, then find the address the host's Tailscale daemon
   will appear from. Connections to the published loopback port reach Caddy from the
   project network's gateway, not from Edge's reserved address:

   ```sh
   docker network inspect llm-gateway-stack_default --format '{{(index .IPAM.Config 0).Gateway}}'
   ```

   Set `LG_TRUSTED_PROXIES` to that address as a `/32` (for example
   `LG_TRUSTED_PROXIES=172.19.0.1/32`) and run `python3 scripts/bootstrap.py` again. An
   untrusted peer's forwarded scheme is discarded and the applications receive
   `X-Forwarded-Proto: http`, which breaks secure cookies and login redirects behind HTTPS.
   Trusting the bridge gateway means every host-local connection to the loopback port can
   assert the scheme; the port is bound to loopback, so only processes on this host can.
3. Serve each port from the host daemon, one command per application, with the port
   from the matching `LG_*_URL`:

   ```sh
   tailscale serve --bg --https=8446 http://127.0.0.1:18080
   tailscale serve --bg --https=8443 http://127.0.0.1:18080
   tailscale serve --bg --https=8444 http://127.0.0.1:18080
   tailscale serve --bg --https=8445 http://127.0.0.1:18080
   tailscale serve --bg --https=8449 http://127.0.0.1:18080
   ```

   `tailscale serve status` lists the result; `tailscale serve --https=<port> --set-path=/ off`
   removes one entry.

Verify from a tailnet device: open the console URL and follow the links, sign in to
Langfuse and LiteLLM's `/ui/`, and open a trace's media. A login that loops back to the
sign-in page means the scheme is not trusted (step 2). Caddy routes each request by its
complete `Host`, including the port, so the forwarding proxy must preserve `Host` (Tailscale
serve does); rewriting the S3 `Host` breaks presigned media and export links. Who can reach
these ports is decided by your tailnet access controls; application login is still required.
See [application URLs](#application-urls) for the rules the origins must follow.

## Corporate certificates and private ACME

`LG_TLS_ISSUER` selects where certificates come from, independently of the access mode:
`internal` (Caddy's own CA, Local Mode), `acme` (Public Mode) or `files` (Local or Public
Mode). Proxy Mode ignores it. Relative `LG_TLS_DIR`, `LG_TLS_CA` and `LG_ACME_CA_ROOT`
paths resolve against this checkout.

Bootstrap records the Compose overlays the issuer needs in the `.env` `COMPOSE_FILE`,
directly after the mode file, and drops overlays an earlier mode or issuer needed. Backups,
restores and direct Compose commands then use the same files. A `COMPOSE_FILE` exported in
the shell gets the overlays for that bootstrap run only; add them to the shell value for
later commands.

### Private or alternative ACME CA

Set `LG_ACME_CA` to the CA's ACME directory URL (`https://`). For a CA whose chain is not
in the public trust stores (step-ca, an ACME-enabled corporate CA), set `LG_ACME_CA_ROOT`
to its CA certificate in PEM form: Caddy trusts it when talking to the ACME server, and
the bootstrap and Checkpoint readiness probes trust it for the issued server certificates.
A CA that requires external account binding takes `LG_ACME_EAB_KEY_ID` and
`LG_ACME_EAB_HMAC`, always together.

```sh
LG_ACCESS_MODE=public
LG_PUBLIC_DOMAIN=gateway.example.internal
LG_TLS_ISSUER=acme
LG_ACME_EMAIL=ops@example.internal
LG_ACME_CA=https://ca.example.internal/acme/acme/directory
LG_ACME_CA_ROOT=/etc/ssl/corp/root_ca.crt
LG_ACME_EAB_KEY_ID=
LG_ACME_EAB_HMAC=
```

Bootstrap selects `compose.acme-ca-root.yaml` (mounts the trust file read-only at
`/certs/acme-ca-root.crt`) and `compose.acme-eab.yaml` when those settings are set.
Only the HTTP-01 and TLS-ALPN-01 challenges are available: the ACME server must reach the
host on TCP 80 and 443 for every HTTPS hostname, and DNS-01 is not offered. Account keys
and issued certificates stay in `caddy-data`.

### Certificate and key files

Put the server certificate chain in `tls.crt` and its unencrypted private key in `tls.key`
inside one directory:

```sh
LG_TLS_ISSUER=files
LG_TLS_DIR=/etc/ssl/llm-gateway
LG_TLS_CA=/etc/ssl/corp/root_ca.crt
```

The certificate must cover every HTTPS hostname: the root domain and the `litellm.`,
`langfuse.`, `s3.` and `rustfs.` subdomains, by name or by a one-label wildcard
(`*.example.com` covers `s3.example.com`, not `example.com`). Configured application URLs
add no certificate names. Bootstrap reads the subject alternative names with `openssl` and
refuses a certificate that leaves a hostname uncovered. `LG_TLS_CA` is the issuing CA in
PEM form for the bootstrap and Checkpoint readiness probes; leave it empty when that CA is
in the host's trust store. Bootstrap selects `compose.files.yaml`, which mounts
`LG_TLS_DIR` read-only at `/certs` and never creates it.

Caddy runs as uid 0 with every capability dropped, so it reads the files by permission
bits: own `tls.key` by root with mode 0600, and keep `tls.crt` and the CA file readable.
Bootstrap reads the mounted files from a throwaway Caddy container, off every network,
before starting, and refuses with `tls_files_unreadable` when Caddy cannot read them, for
example a key another user owns with mode 0600, or a symlink that points outside the
directory. In Local Mode, `127.0.0.1` keeps its internal-CA certificate; the application
hostnames use the files.

Replace a certificate by writing the new pair into the directory, then restart Caddy and
verify the handshake:

```sh
docker compose restart caddy
python3 scripts/bootstrap.py
```

The gateway runs Caddy with its admin API off, so `caddy reload` is unavailable and the
restart briefly interrupts requests. Caddy does not watch the directory or renew file
certificates; renew them with your PKI before they expire. Certificate files are outside
the Checkpoint; back them up with your PKI.

## Shared host

When the backplane or the observability stack runs on the same host, the gateway joins the
external Docker network `platform` as `lg-gateway`; LiteLLM stays on the project network
and is reachable only through the gateway. With `LG_METRICS=true` the datastore exporters
also join as `lg-valkey-exporter:9121` and `lg-postgres-exporter:9187`; the datastores
remain private.

The Platform Network has one allocation on every host, defined in the shared contract
([conventions](../conventions.md)): subnet `172.30.0.0/24` (`LG_PLATFORM_SUBNET`), dynamic
range `172.30.0.128/25` (`LG_PLATFORM_IP_RANGE`) and gateway `172.30.0.1`. Platform Edge
holds the reserved address `172.30.0.2` outside the dynamic range. Whichever bootstrap runs
first creates the network with these parameters. Every bootstrap validates an existing
network and refuses a different subnet or range, or a network with no IPAM configuration,
with `platform_network_mismatch`. To repair a network created before this contract, stop
every stack on it, run `docker network rm` on the network the error names
(`LG_PLATFORM_NETWORK`, default `platform`), then rerun bootstrap. Every stack on the host
must use the same values.

Two stacks cannot both publish 80 and 443. On a shared host Platform Edge owns those ports,
terminates TLS, and routes the application hostnames to `lg-gateway:80`. Edge's bundle
installer (`python3 scripts/bootstrap.py --with gateway` in the Edge checkout) writes the
settings below and runs this stack's bootstrap; to do it by hand, set:

```sh
LG_ACCESS_MODE=proxy
LG_PUBLIC_DOMAIN=example.com
LG_HTTP_PORT=18080
```

`LG_BIND_HOST` may also be an explicit interface address; wildcard binds (`0.0.0.0` and
`::`) are refused in Proxy Mode. Behind another gateway, the stack publishes no HTTPS port
and issues no certificates. The application scheme defaults to HTTPS; `LG_SCHEME=http`
remains useful when the gateway in front serves HTTP.

`LG_TRUSTED_PROXIES` defaults to Edge's reserved address, `172.30.0.2/32`, so no address
discovery is needed. An empty value uses the same default. Change it only for another
gateway or a different Platform Network subnet, and keep it to exact addresses (`/32` for
IPv4, `/128` for IPv6). Broader ranges trust every peer in that range; avoid them on shared
networks, since private address space can include other tenants. Caddy preserves trusted
`X-Forwarded-Proto`; an untrusted caller cannot assert it. Docker never assigns the
reserved address dynamically, so without Edge the default grants nothing. Bootstrap
refuses an `LG_PLATFORM_IP_RANGE` that contains a trusted IPv4 proxy address.

## Application URLs

Each application has one configured browser origin, used for login, redirects, presigned
links and CORS. Empty `LG_CONSOLE_URL`, `LG_LITELLM_URL`, `LG_LANGFUSE_URL`, `LG_S3_URL` and
`LG_RUSTFS_URL` derive the origin from the mode, domain and port suffix. Set them in full
when the applications share one hostname on different ports (see [Tailscale](#tailscale))
or when another gateway presents them under other names.

Each URL must contain `http://` or `https://`, a DNS hostname or IPv4 address, and an
optional port from 1 to 65535. Omit trailing slashes, paths, credentials, queries and
fragments. Use ordinary decimal ports without leading zeroes. Public Mode requires HTTPS.
Applications must have distinct authorities (hostname plus port); default ports 80 for
HTTP and 443 for HTTPS are treated as omitted. A URL cannot claim another application's
hostname.

These settings choose browser origins; they do not publish additional Docker ports or
issue certificates for extra hostnames. Proxy Mode routes the configured authorities
exactly, by the complete request `Host`, and keeps the hostname routes. Local and Public
Mode keep their listeners and certificate names.

Langfuse uses its URL for login and the S3 URL for browser media and export links. RustFS
allows browser media requests from the configured Langfuse origin. Internal ingestion
continues over the Compose network. LiteLLM receives its URL as the documented
[`PROXY_BASE_URL`](https://docs.litellm.ai/docs/proxy/config_settings), which controls its
external origin for redirects and secure cookies. The Stack Console reads these URLs from
`/origins.json`.

Bootstrap canonicalizes URL hostnames and default ports and appends a canonical
`LG_LANGFUSE_URL` when needed, so RustFS CORS uses the exact origin browsers send, while
preserving existing env lines. When invoking Compose directly with shell URL overrides,
use lowercase hostnames and omit `:80` for HTTP or `:443` for HTTPS.

## Operator access and health

Applications authenticate themselves. LiteLLM's admin UI at `litellm.<domain>/ui/` and
its `/openapi.json` sit behind LiteLLM's login (`UI_USERNAME`, `UI_PASSWORD`), the RustFS
console behind RustFS's login, Langfuse behind its own. The gateway has no address-based
operator layer: Docker port forwarding presents the bridge address instead of loopback,
and behind Platform Edge every request arrives from Edge, so a socket-peer allow list
would either lock the operator out or admit every Edge client. Edge, Tailscale or your
firewall controls who can reach the gateway at all; the applications control who may act.

Health probes (`/health/<component>` on the root hostname, `/health/*` on the application
hostnames) return the upstream status with an empty body for every client: 200 when the
probe passes, 503 when the upstream cannot answer, 404 for an unknown component. The
public Stack Console shows service health and the configured versions from `/status.json`;
see [status document](maintenance.md#status-document) for the component list.

## Metrics

The gateway's unpublished listener on port 8081 serves two scrape paths on the Platform
Network, both restricted to socket peers in `LG_CHECKPOINT_ALLOW`:

| Target | Serves | Job |
| --- | --- | --- |
| `http://lg-gateway:8081/metrics` | Checkpoint metrics | `llm-gateway-checkpoints` |
| `http://lg-gateway:8081/metrics/litellm` | LiteLLM's Prometheus metrics | `llm-gateway` |

Add each scraper's own address to `LG_CHECKPOINT_ALLOW` as an exact IP (`/32` or `/128`);
a wider range would admit every peer in it. Other peers receive 404. This setting grants only metrics access. Edge routes to port 80
and receives no metrics there. The same listener answers `/health/status` over loopback
for Caddy's container healthcheck in every access mode. LiteLLM is not on the Platform
Network; `/metrics/litellm` through the gateway is the only scrape path.

The datastore exporters run only with the Compose profile `metrics`. Set `LG_METRICS=true`
and rerun bootstrap; it records `metrics` in `COMPOSE_PROFILES` and keeps any other
profiles listed there. A shell `LG_METRICS` that differs from the saved value is saved with
the profile. Set `LG_METRICS=true` whenever Observability scrapes this stack; Platform
Edge's bundle installer sets it when Observability is selected. Bootstrap never removes
running exporters, and a Checkpoint refuses a running container its selected profiles do
not include. After setting `LG_METRICS=false`, either keep the exporters with
`LG_METRICS=true` and a bootstrap run before the next Checkpoint, or remove them:
`docker compose --profile metrics rm --stop --force valkey-exporter postgres-exporter`.

The observability stack must configure both 8081 scrapes plus `lg-valkey-exporter:9121`
(job `llm-gateway-valkey`) and `lg-postgres-exporter:9187` (job `llm-gateway-postgres`);
verify these jobs before relying on their alerts.

## RustFS admin console

The RustFS admin console is always on, at `http://rustfs.localhost` locally or
`https://rustfs.<your-domain>` with public HTTPS. Sign in with the installation's
`RUSTFS_ACCESS_KEY` and `RUSTFS_SECRET_KEY` from its private `.env`. Keep those values
private. The S3 API on `s3.<domain>` is separate and serves presigned media and exports.
`LG_RUSTFS_URL` sets the console's full browser origin when another gateway handles HTTPS.

## What is never published

PostgreSQL, ClickHouse, Valkey and the RustFS API on its internal port are reachable only
inside the Compose network. LiteLLM's `/metrics` is served without authentication because
only this project's Compose network can reach it. Caddy answers 404 for `/metrics` on the
LiteLLM application listener in every mode; scrapers read it through the gateway's
unpublished listener (see [metrics](#metrics)).

## Troubleshooting

| Symptom | Cause and fix |
| --- | --- |
| Caddy exits with `Conflicting access settings` | `docker/caddy/access-mode.sh` refused the mode, scheme, domain or an `LG_*_URL`. The message names the setting; fix it in `.env` and rerun bootstrap. |
| `platform_network_mismatch` | The `platform` network exists with another subnet or range. Stop every stack on it, `docker network rm platform`, rerun bootstrap. |
| `tls_files_unreadable` | Caddy cannot read `tls.key`, `tls.crt` or the CA file inside `LG_TLS_DIR`. Check ownership, mode bits and symlink targets. |
| Bootstrap reports `CERTIFICATE_VERIFY_FAILED` | The issuing CA is not in the host's trust store. Set `LG_TLS_CA` (or `LG_ACME_CA_ROOT` for a private ACME CA) to its PEM file. |
| Proxy Mode returns 404 for an application | The request `Host` (including port) matches no configured hostname or `LG_*_URL` authority. Compare `/origins.json` with the URL the browser used. |
| Langfuse login loops or presigned links fail | The application origin differs from the browser's URL. Set the matching `LG_LANGFUSE_URL` or `LG_S3_URL` and rerun bootstrap. |
