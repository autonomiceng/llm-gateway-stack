# Ingress and access modes

Caddy is the only published entry. By default, applications use these hostnames under one domain:

| Hostname | Upstream |
| --- | --- |
| `<domain>` | Stack Console and `/health/*` probes |
| `litellm.<domain>` | LiteLLM API; admin UI restricted to operators |
| `langfuse.<domain>` | Langfuse |
| `s3.<domain>` | RustFS S3 API, for presigned media and export URLs |
| `rustfs.<domain>` | RustFS admin console, enabled by default; operators only |

## Local Mode (default)

`LG_ACCESS_MODE=local` serves HTTP and self-signed HTTPS on `LG_BIND_HOST=127.0.0.1`.
Both protocols work simultaneously without HTTP redirects or HSTS, including HSTS from
upstream applications. The application URL protocol defaults to HTTP. Browsers and
systemd-resolved hosts resolve `*.localhost`; elsewhere configure DNS or use curl
`--resolve`. Nothing listens outside the host by default.

The root console accepts `localhost`, `127.0.0.1`, and arbitrary HTTP Host values.
Applications still require the explicit names in the table. Console links come from
configured origins, including when the root is opened by IP; Langfuse authentication,
S3 signatures and CORS do not acquire arbitrary aliases.

If another service owns port 80 or 443, set `LG_HTTP_PORT` and `LG_HTTPS_PORT`, and set `LG_PUBLIC_PORT_SUFFIX` to the application's browser-facing port (for example `:8080`) so Langfuse's login URL, presigned S3 URLs and CORS carry it. URLs then look like `http://litellm.localhost:8080`. Optional `LG_SCHEME=https` selects HTTPS as the configured application URL and needs the HTTPS port suffix instead; it does not disable HTTP. The second listener provides transport access, while applications retain a single configured application URL.

The template's `COMPOSE_FILE=compose.yaml:compose.${LG_ACCESS_MODE:-local}.yaml`
selects mode defaults and publishing. Use Compose 2.24.4 or newer. Keep that assignment
when changing modes, and recreate services with `python3 scripts/bootstrap.py`.
Explicit `-f` or `COMPOSE_FILE` overrides must include the matching mode file.
For a lasting deployment choice, edit `.env`. Shell overrides apply only to that command;
bootstrap does not save them into existing settings. Use the same overrides for backup
and restore, or save them in `.env` first.
Issuer and listener scheme are derived from the mode and are not operator settings.

## Public Mode

Set DNS A or AAAA records for the four default hostnames to this host, open ports 80 and 443, and in `.env`:

```sh
LG_PUBLIC_DOMAIN=gateway.example.com
LG_ACCESS_MODE=public
LG_BIND_HOST=0.0.0.0
```

Then `python3 scripts/bootstrap.py`. Leave `LG_SCHEME` empty for the HTTPS default;
Public mode requires HTTPS application URLs. Caddy proves ownership of your domain through port 80,
stores certificates in `caddy-data`, and redirects HTTP to configured HTTPS origins.
Root `/health/*` probes remain available over HTTP. Redirects for unknown HTTP hosts use
the configured root domain. Application login, presigned URLs and CORS use that same
configured scheme, domain and port.

Local Mode issues and renews certificates from Caddy's own CA in the existing
`caddy-data` volume. To use local HTTPS, export only its public root certificate and
install it on clients that need this stack:

```sh
docker compose cp caddy:/data/caddy/pki/authorities/local/root.crt ./gateway-root.crt
```

Bootstrap and restore read this root from their own Caddy container for local HTTPS
health probes. Both use the configured public domain for TLS hostname verification.
Never disable certificate verification in clients instead.
No startup command changes host trust. Never copy CA private keys or certificate
volumes between stacks; select `proxy` when Platform Edge handles HTTPS.

## What is never published

PostgreSQL, ClickHouse, Valkey and the RustFS API on its internal port are reachable only inside the Compose network. LiteLLM's `/metrics` is served without authentication because only the Docker networks can reach it. Caddy answers 404 for `/metrics` on the LiteLLM application listener in every mode; the observability stack scrapes `lg-litellm:4000/metrics` directly.

## Shared host

When the backplane or the observability stack runs on the same host, the gateway joins the external Docker network `platform` as `lg-gateway` and LiteLLM as `lg-litellm`. The monitoring sidecars also join as `lg-valkey-exporter:9121` and `lg-postgres-exporter:9187`; the datastores remain private. Bootstrap creates the network if it is missing; `docker network create platform` does the same by hand.

Two stacks cannot both publish 80 and 443. On a shared host Platform Edge owns those ports,
terminates TLS, and routes explicit application hostnames to `lg-gateway:80`.
Set `LG_ACCESS_MODE=proxy`, the external `LG_PUBLIC_DOMAIN`, and a spare
`LG_HTTP_PORT` on loopback. An explicit interface address in `LG_BIND_HOST` is also
supported; wildcard binds (`0.0.0.0` and `::`) are refused in Proxy Mode.
Behind another gateway, the stack publishes no HTTPS port and performs no TLS
issuance. The external scheme defaults to HTTPS; `LG_SCHEME=http` remains useful
when the edge's configured application URL is HTTP.

Set `LG_TRUSTED_PROXIES` to Edge’s reserved address (`/32` for IPv4, `/128` for IPv6).
Broader ranges deliberately trust every peer in that range; avoid them for shared networks.
Caddy preserves trusted `X-Forwarded-Proto`; an untrusted caller cannot assert it.
Running behind another gateway requires a nonempty trust list. Trust only networks you control; private
address space can include other tenants. Standalone modes normally leave it empty.

## One Tailscale hostname with separate ports

In Proxy Mode, set full browser origins when the applications share one hostname:

```dotenv
LG_ACCESS_MODE=proxy
LG_CONSOLE_URL=https://darkforge.tail694fe2.ts.net:8446
LG_LITELLM_URL=https://darkforge.tail694fe2.ts.net:8443
LG_LANGFUSE_URL=https://darkforge.tail694fe2.ts.net:8444
LG_S3_URL=https://darkforge.tail694fe2.ts.net:8445
```

Keep the template's `COMPOSE_FILE` setting. Set `LG_TRUSTED_PROXIES` to Edge's address
and choose a free loopback `LG_HTTP_PORT`. Platform Edge must serve HTTPS on these four
ports and forward each request to `lg-gateway:80`. It must preserve the complete `Host`,
including the port, and set `X-Forwarded-Proto` to `https`. Rewriting the S3 Host breaks
presigned media and export links. Opening firewall ports alone does not configure Edge.

Each URL must contain `http://` or `https://`, a DNS hostname or IPv4 address, and an
optional port from 1 to 65535. Omit trailing slashes, paths, credentials, queries and
fragments. Use ordinary decimal ports without leading zeroes. Public Mode requires HTTPS.
Applications must have distinct authorities, meaning hostname plus port; default ports
80 for HTTP and 443 for HTTPS are treated as omitted. A URL cannot claim another
application's existing hostname, including the optional RustFS admin hostname.

Empty values keep the existing scheme, domain and port defaults in every mode. These
settings choose browser origins; they do not publish additional Docker ports or issue
certificates for extra hostnames. Proxy Mode routes the configured authorities exactly
and retains the existing hostname routes. Local and Public Mode retain their existing
listeners and certificate names.

Langfuse uses its URL for login and the S3 URL for browser media and export links. RustFS
allows browser media requests from the configured Langfuse origin. Internal ingestion
continues over the Compose network. LiteLLM receives its URL as the documented
[`PROXY_BASE_URL`](https://docs.litellm.ai/docs/proxy/config_settings), which controls its
external origin for redirects and secure cookies. `PUBLIC_URL` is not used. The Stack
Console reads these URLs from `/origins.json`.

The existing access rules still apply. LiteLLM API calls need their usual keys; users
need their usual app login. LiteLLM `/ui*` remains operator-restricted, and trusting Edge
alone does not grant UI access. Any deliberate operator access through Edge requires
its socket address in `LG_OPERATOR_ALLOW` and matching access restrictions at Edge.

## Operator access

`LG_OPERATOR_ALLOW` is a space-separated list of socket peer CIDRs, default
`127.0.0.0/8 ::1`. It gates `/versions.json`, health JSON bodies, LiteLLM `/ui*` and
`/openapi.json`, and the RustFS console. Other clients receive 404 for operator
paths; health probes preserve the upstream HTTP status with an empty body. The public
Stack Console can still show service health; pinned versions require operator access.
Forwarded client headers never grant operator access.

Docker port forwarding can present the host's bridge address instead of loopback. Add
that specific address to the allow list when needed for local operator access. Behind
platform-edge, socket peers are the edge: allowing the edge's subnet would also allow its
public callers. Keep public operator-path exclusions at the edge and use a direct local
connection for administration. `LG_TRUSTED_PROXIES` does not grant operator access.

The RustFS console is enabled by default. Set `LG_RUSTFS_CONSOLE=off` and recreate
RustFS and Caddy to disable it. The S3 API remains available for presigned media and
exports. Edge can route its separate admin hostname or private Tailscale port; application
login and the gateway's operator allow list still apply.

Checkpoint metrics are available at `http://lg-gateway:8081/metrics` on the platform
network, under job `llm-gateway-checkpoints`. Add the scraper's address to
`LG_CHECKPOINT_ALLOW`, or its dedicated scraper network CIDR. This setting grants only
checkpoint metrics access; keep operator sources in `LG_OPERATOR_ALLOW`. Port 8081 is an unpublished container listener; the edge routes to
port 80 and receives no checkpoint metrics there. The same listener serves
`/versions.json` over loopback for Caddy's healthcheck in every TLS mode. Include loopback
in `LG_OPERATOR_ALLOW` so that healthcheck continues to work.

The observability stack must configure the checkpoint scrape plus
`lg-valkey-exporter:9121` (job `llm-gateway-valkey`) and `lg-postgres-exporter:9187`
(job `llm-gateway-postgres`); verify these jobs before relying on their alerts.

`LG_GRAFANA_URL` and `LG_BACKPLANE_URL` optionally set the companion links in the gateway overview. They do not install those stacks or add application routes. Platform Edge’s Tailscale setup fills them in automatically.

Bootstrap canonicalizes application URL hostnames and default ports. It appends a canonical `LG_LANGFUSE_URL` override when needed, so RustFS CORS uses the exact origin sent by browsers, while preserving existing env lines. If invoking Compose directly with shell URL overrides, use lowercase hostnames and omit `:80` for HTTP or `:443` for HTTPS. Non-default ports remain explicit.

## RustFS browser admin console

The RustFS admin console is enabled by default at `http://rustfs.localhost` locally,
or `https://rustfs.<your-domain>` with public HTTPS. Sign in using the installation's
`RUSTFS_ACCESS_KEY` and `RUSTFS_SECRET_KEY` from its private `.env`. Keep those values private.
The console retains the same `LG_OPERATOR_ALLOW` restriction as other operator pages.
Set `LG_RUSTFS_CONSOLE=off` to disable it. This does not disable the S3 API.

For access through Platform Edge and Tailscale, rerun Edge's `scripts/tailscale_serve.py`
after updating both repositories. It connects the admin console at HTTPS port 8449 by
default, independently of the S3 API on 8445. The gateway overview shows the current
console setting and refreshes application addresses along with health every 30 seconds.
`LG_RUSTFS_URL` sets a full browser origin when another gateway handles HTTPS.
