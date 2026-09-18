# Ingress and access modes

Caddy is the only published entry. Every application has a fixed hostname under one domain:

| Hostname | Upstream |
| --- | --- |
| `<domain>` | Stack Console and `/health/*` probes |
| `litellm.<domain>` | LiteLLM API; admin UI restricted to operators |
| `langfuse.<domain>` | Langfuse |
| `s3.<domain>` | RustFS S3 API, for presigned media and export URLs |
| `rustfs.<domain>` | RustFS console, disabled unless `LG_RUSTFS_CONSOLE=on`; operators only |

## Local Mode (default)

`LG_PUBLIC_DOMAIN=localhost`, `LG_SCHEME=http`, `LG_BIND_HOST=127.0.0.1`. Browsers and systemd-resolved hosts resolve *.localhost; elsewhere pass -H 'Host: ...'. Nothing listens outside the host. Other machines cannot reach the stack, by design; use an SSH tunnel to port 80 if you need to look from elsewhere.

If another service owns port 80 or 443, set `LG_HTTP_PORT` and `LG_HTTPS_PORT`, and set `LG_PUBLIC_PORT_SUFFIX` to the port browsers will use (for example `:8080`) so Langfuse's login URL, presigned S3 URLs and the CORS origin carry it. URLs then look like `http://litellm.localhost:8080`.

## Public Mode

Set DNS A or AAAA records for the four default hostnames to this host, open ports 80 and 443, and in `.env`:

```sh
LG_PUBLIC_DOMAIN=gateway.example.com
LG_SCHEME=https
LG_TLS_ISSUER=acme
LG_BIND_HOST=0.0.0.0
```

Then `docker compose up -d`. Caddy answers the ACME challenge on port 80, stores certificates in the `caddy-data` volume, and redirects HTTP to HTTPS. Langfuse's login URL, its presigned S3 URLs and RustFS's CORS origin all derive from the same two settings, so nothing else needs editing.

`LG_TLS_ISSUER=internal` makes Caddy issue certificates from its own CA, for private networks with internal DNS. Export the root certificate and install it on each client:

```sh
docker compose cp caddy:/data/caddy/pki/authorities/local/root.crt ./gateway-root.crt
```

Bootstrap and restore read this root from their own Caddy container for local HTTPS
health probes. Both use the configured public domain for TLS hostname verification.
Never disable certificate verification in clients instead.

## What is never published

PostgreSQL, ClickHouse, Valkey and the RustFS API on its internal port are reachable only inside the Compose network. LiteLLM's `/metrics` is served without authentication because only the Docker networks can reach it. Caddy answers 404 for `/metrics` on the LiteLLM hostname in both modes; the observability stack scrapes `lg-litellm:4000/metrics` directly.

## Shared host

When the backplane or the observability stack runs on the same host, the gateway joins the external Docker network `platform` as `lg-gateway` and LiteLLM as `lg-litellm`. The monitoring sidecars also join as `lg-valkey-exporter:9121` and `lg-postgres-exporter:9187`; the datastores remain private. Bootstrap creates the network if it is missing; `docker network create platform` does the same by hand.

Two stacks cannot both publish 80 and 443. On a shared host the `platform-edge` project owns those ports, terminates TLS, and routes each hostname to the stack's own Caddy over the platform network as `lg-gateway:80`. Configure this stack with the public values (`LG_PUBLIC_DOMAIN`, `LG_SCHEME=https`) so Langfuse's login and presigned URLs are right, plus `LG_LISTEN_SCHEME=http` and `LG_TLS_ISSUER=none` so its Caddy listens on plain HTTP behind the edge, and spare `LG_HTTP_PORT` and `LG_HTTPS_PORT` on `LG_BIND_HOST=127.0.0.1`. Set `LG_TRUSTED_PROXIES` to the platform subnet so Caddy accepts the edge's `X-Forwarded-Proto`. Leave it empty for standalone deployments. Trust only networks you control; private address space can include other tenants.

## Operator access

`LG_OPERATOR_ALLOW` is a space-separated list of socket peer CIDRs, default
`127.0.0.0/8 ::1`. It gates `/versions.json`, health JSON bodies, LiteLLM `/ui*` and
`/openapi.json`, and the optional RustFS console. Other clients receive 404 for operator
paths; health probes preserve the upstream HTTP status with an empty body. The public
Stack Console can still show service health; pinned versions require operator access.
Forwarded client headers never grant operator access.

Docker port forwarding can present the host's bridge address instead of loopback. Add
that specific address to the allow list when needed for local operator access. Behind
platform-edge, socket peers are the edge: allowing the edge's subnet would also allow its
public callers. Keep public operator-path exclusions at the edge and use a direct local
connection for administration. `LG_TRUSTED_PROXIES` does not grant operator access.

The RustFS console is disabled by default. Set `LG_RUSTFS_CONSOLE=on` and recreate
RustFS and Caddy to enable its operator-restricted hostname. The S3 API remains available
for presigned media and exports. platform-edge must drop its public `rustfs` route;
access an enabled console through the gateway's direct local listener.

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
