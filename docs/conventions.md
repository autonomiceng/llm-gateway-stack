<!-- vendored from platform-edge@749bdf6 ; do not edit here -->
# Stack conventions

How the four repos (llm-gateway-stack, agent-backplane, observability-stack and
platform-edge) are laid out and operated, so that deploying any of them feels the same.

This file is canonical in platform-edge at `docs/conventions.md` and vendored into each
sibling repo at the same path by `scripts/sync-conventions.sh`. A vendored copy starts with
one HTML comment naming the platform-edge commit it was copied from; that commit is the
contract version. Siblings never edit their copy: change platform-edge, then re-vendor.
`scripts/sync-conventions.sh --check <sibling-dir>` reports drift.

Contract revision: 2026-09-24 (plan v2, red team v1, corrections after stages 1 to 6).

## Repository

- `compose.yaml` at the root with `name:` set. Optional pieces are `profiles:`; overlay files
  only where a profile cannot express it. `docker compose up` with no flags starts the core.
- Shipped image defaults are pinned inline as `image:tag@sha256`. Renovate proposes bumps;
  a human merges after the stack's smoke contract passes. Complete image references may
  override these defaults through stack-prefixed env settings for unvalidated local
  experiments. Shipped defaults use no floating tags or `latest`.
- `.env.example` lists every operator setting: one comment line, then the assignment. No
  secrets in the template; bootstrap generates them. Stack-owned settings carry a prefix
  (`LG_`, `BP_`, `OB_`, `PE_`); upstream applications keep their upstream variable names.
  Below, `*_` stands for the stack prefix.
- `mise.toml` pins the toolchain. `scripts/` holds bootstrap, validate, smoke, backup and
  restore. `docs/DESIGN.md` is the map, `docs/adr/` the decisions, `CONTEXT.md` the glossary,
  `docs/operations/` the runbooks, `docs/agents/` the guidance for agents.
- `AGENTS.md` at the root, `CLAUDE.md` containing `@AGENTS.md`. CONTRIBUTING, SECURITY, PR
  and issue templates in the same shape across repos.
- Default branch `main`. Conventional Commits. `Co-Authored-By: Various Models`.
- `.scratch/`, `.agents/`, `.devloop/`, `.plans/` are gitignored. Plans never commit.

## Platform contract

The interfaces the four stacks share. Later work quotes this table; an implementation that
differs from it is a contract change, made here first.

| Item | Contract | Settings |
| --- | --- | --- |
| Platform Network | External Docker network `platform`. Whichever bootstrap runs first creates it with `--subnet 172.30.0.0/24 --ip-range 172.30.0.128/25 --gateway 172.30.0.1`; the gateway is the first host address of the subnet and is not configured separately. Every bootstrap validates an existing network's IPAM and refuses a mismatch (exit 1) with a message naming the expected and actual values. | `*_PLATFORM_NETWORK` (default `platform`), `*_PLATFORM_SUBNET` (default `172.30.0.0/24`), `*_PLATFORM_IP_RANGE` (default `172.30.0.128/25`) |
| Edge address | `pe-edge` at `172.30.0.2`, outside the dynamic range, set as `ipv4_address` in Edge `compose.yaml`. | `PE_EDGE_IP` (default `172.30.0.2`) |
| Trusted proxies | Gateway and Observability trust exactly Edge's address. Backplane has no trusted-proxy setting: Edge reaches its server directly and the server takes its browser origin from `BP_PUBLIC_URL`. Edge trusts nothing by default. Metrics allowlists stay exact IP. | `LG_TRUSTED_PROXIES`, `OB_TRUSTED_PROXIES` (default `172.30.0.2/32`); `PE_TRUSTED_PROXIES` (default empty); `PE_METRICS_ALLOW` |
| Ingress aliases | `lg-gateway:80`, `ob-gateway:80`, `bp-server:3000`. Backplane has no internal gateway behind Edge; Edge routes `backplane.` to its server. Aliases are interfaces: renaming one needs a migration. | none |
| Metrics interfaces | See the metrics table below. Only these endpoints join the Platform Network; datastores never do. | `OB_SCRAPE_EDGE`, `OB_SCRAPE_GATEWAY`, `OB_SCRAPE_BACKPLANE`, `PE_METRICS_ALLOW`, `LG_CHECKPOINT_ALLOW`, `LG_METRICS` (default `false`), `BP_OPERATIONS_TOKEN`, `OB_BACKPLANE_OPERATIONS_TOKEN` |
| Hostnames | Root, `litellm.`, `langfuse.`, `s3.`, `rustfs.`, `backplane.` and `grafana.` under one public domain. Application hostnames do not change between access modes. Backplane also configures its full browser origin, which is the required input behind Edge. | `PE_PUBLIC_DOMAIN`, `LG_PUBLIC_DOMAIN`, `OB_PUBLIC_DOMAIN`, `BP_PUBLIC_DOMAIN` (standalone hostname and certificate), `BP_PUBLIC_URL` (browser origin); `PE_ROOT_HOST` (console hostname label: names the console's Tailscale node, `platform` when empty; reserved as an optional public-domain prefix when the apex is not routable, not yet implemented; default empty) |
| Bundle ports | Behind Edge, each stack listens on loopback HTTP: 18080 Gateway, 18180 Observability, 3000 Backplane. Edge `bootstrap.py --with <stack>` writes these ports into the sibling's `.env` together with `proxy` mode, the public domain, scheme and an empty port suffix (Gateway, Observability), the Platform Network allocation, the trusted proxy `PE_EDGE_IP/32`, `LG_METRICS=true` when Observability is also selected, and the browser-origin keys (Tailnet Origins), then runs the sibling's `python3 scripts/bootstrap.py`. It still writes `BP_TRUSTED_PROXIES`, `OB_GATEWAY_HEALTH_HOST`, `OB_GATEWAY_URL` and `OB_BACKPLANE_URL`, which current siblings ignore. | `*_BIND_HOST` (default `127.0.0.1`), `LG_HTTP_PORT=18080`, `OB_HTTP_PORT=18180`, `BP_PORT=3000`, `LG_PUBLIC_PORT_SUFFIX` and `OB_PUBLIC_PORT_SUFFIX` (empty) |
| Access modes | `local`: HTTP and internal-CA HTTPS on loopback, no redirect, no HSTS. `public`: trusted HTTPS with HTTP redirect; the bind address is chosen explicitly. `proxy`: HTTP only, behind Edge or another gateway that handles HTTPS. Every stack keeps standalone `public`. The canonical application scheme is a separate setting. | `*_ACCESS_MODE` (default `local`), `*_SCHEME` (`LG_`, `OB_`, `PE_`; Backplane derives it from `BP_PUBLIC_URL`) |
| Tailnet Origins | Optional. Edge runs one `tailscale/tailscale` node per routed hostname (`compose.tailscale.yaml`, profiles `ts-<name>`, external volume `${PE_VOLUME_PREFIX}_ts-<name>`); each serves `https://<name>.<tailnet>.ts.net` with a Tailscale certificate and proxies to `pe-edge:80` with the original Host. Edge sets `X-Forwarded-Proto: https` for requests from the Platform Network's dynamic range and trusts no forwarded header. `bootstrap.py --tailscale` records the selection and the tailnet domain in `.env`; the bundle writes the origins into the sibling browser-origin settings (`LG_CONSOLE_URL`, `LG_LITELLM_URL`, `LG_LANGFUSE_URL`, `LG_S3_URL`, `LG_RUSTFS_URL`, `OB_GRAFANA_URL`, `BP_PUBLIC_URL`). All nodes form one authorization domain. | `PE_TS_AUTHKEY` (secret), `PE_TS_TAG` (template `tag:platform`), `PE_TS_APPS` (default all seven), `PE_TAILNET_DOMAIN` (recorded by bootstrap) |
| TLS issuers | `internal`: the stack Caddy's own CA (default in `local`). `acme`: an ACME directory (default in `public`; Let's Encrypt when no directory is set). `files`: an operator directory mounted read-only at `/certs` containing `tls.crt` and `tls.key`; bootstrap validates that the SAN list covers every configured hostname; replacement is swap files then `docker compose exec caddy caddy reload`; a stack Caddy with `admin off` has no reload, so it replaces certificates by `docker compose restart caddy` followed by bootstrap. Bootstrap's own HTTPS readiness probe trusts the CA file when one is given. Public ACME needs TCP 80 and 443 reachable; DNS-01 is not offered. | `*_TLS_ISSUER` (`internal`, `acme`, `files`), `*_ACME_EMAIL`, `*_ACME_CA` (directory URL), `*_ACME_CA_ROOT` (trust file for a private ACME server), `*_ACME_EAB_KEY_ID`, `*_ACME_EAB_HMAC`, `*_TLS_DIR`, `*_TLS_CA` |
| Status v2 | Each stack serves `GET /status.json` (schema in "Status v2") and `GET /health/<component>` (status only, empty body publicly). Gateway, Observability and Edge write a static file at bootstrap; Backplane serves it from the server through an explicit public projection. Consumers show tags as configured, never running. No host observers, timers or `docker exec`. | none |
| Secrets | Each bootstrap generates its missing secrets once into `.env`: mode 0600, atomic write, unrelated lines preserved byte for byte, present values never rewritten, and a refusal when installation state exists but a secret is missing. Secrets never appear in argv, container labels or world-readable rendered files; a password a service would otherwise take on its command line is injected as a Compose config file (Gateway's Valkey). No secret manager; `sops` or `infisical run` are documented options for off-host storage. | none |
| Bootstrap | `python3 scripts/bootstrap.py` in every repo, standard library only. Required flags: `--dry-run` (render and validate, write nothing) and `--env-file <path>` (default `.env`). Exit codes: 0 ready, 1 refused, 2 usage, 3 not ready. Errors are one JSON line on stderr. Once `.env` exists, plain `docker compose up` starts the stack. Backplane's bootstrap also takes `--capability-file <path>` on a fresh installation, exports the pending enrollment capability there and prints the enrollment command, which runs in a container from the server image (`compose.enroll.yaml`), so the host needs only Docker and Python. | none |
| UI kit | `platform-ui`: `platform.css` (tokens plus header, card, badge and endpoint components) and `docs/ui-kit.md`, canonical in platform-edge, vendored with the source revision and a checksum. Layout and application styles stay local. | none |

### Metrics interfaces

| Endpoint | Serves | Access | Notes |
| --- | --- | --- | --- |
| `pe-edge:80/metrics` | Edge Caddy metrics | exact-IP allowlist `PE_METRICS_ALLOW` | scraped when `OB_SCRAPE_EDGE=true` |
| `lg-gateway:8081/metrics` | Gateway checkpoint metrics | exact-IP allowlist `LG_CHECKPOINT_ALLOW` (template default loopback only; add the scraper's address) | scraped when `OB_SCRAPE_GATEWAY=true` |
| `lg-gateway:8081/metrics/litellm` | LiteLLM metrics | exact-IP allowlist `LG_CHECKPOINT_ALLOW`, as above | scraped when `OB_SCRAPE_GATEWAY=true`; the only LiteLLM scrape path, since LiteLLM is not on the Platform Network |
| `lg-valkey-exporter:9121`, `lg-postgres-exporter:9187` | Gateway datastore exporters | Platform Network only | Compose profile `metrics`, recorded by bootstrap when `LG_METRICS=true`; the bundle sets it when Observability is selected |
| `bp-server:3000/metrics` | Backplane metrics | bearer token `BP_OPERATIONS_TOKEN`, held by Observability as `OB_BACKPLANE_OPERATIONS_TOKEN` | scraped when `OB_SCRAPE_BACKPLANE=true` (O0); the token value never appears in a label |
| `ob-*` | Observability backends | not scraped externally | Observability observes itself |

## Status v2

Contract version 2 replaces the version 1 observation model. Each stack publishes what it
was configured with; liveness comes from separate health paths. The full field rules and
per-stack examples are in platform-edge `docs/operations/status-contract.md`.

```json
{
  "contract": 2,
  "stack": "gateway",
  "configuredAt": "2026-09-23T16:00:00Z",
  "components": [
    {"id": "litellm", "name": "LiteLLM", "kind": "app", "enabled": true,
     "image": "ghcr.io/berriai/litellm:v1.101.0", "version": "v1.101.0",
     "health": "/health/litellm", "url": "https://litellm.example.com"},
    {"id": "postgres", "name": "PostgreSQL", "kind": "datastore", "enabled": true,
     "image": "postgres:18.6", "version": "18.6", "health": "/health/postgres"}
  ],
  "features": {"backups": {"configured": true, "lastCheckpointAt": null},
               "alerts": {"configured": false}}
}
```

Rules:

- Closed field set. The envelope has exactly `contract`, `stack`, `configuredAt`, `components`
  and `features`. A component has exactly `id`, `name`, `kind`, `enabled`, `image`, `version`,
  `health` and optional `url`. `features` has only `backups` and `alerts`. Producers emit
  nothing else; a consumer treats any other field as a malformed document. Adding a field is
  a contract bump.
- `enabled` reflects the selected Compose profiles and overlays at configuration time.
  Disabled components are shown as off and never probed.
- `health` is always the same-origin path `/health/<id>`. `GET` returns 200 when the
  component's documented bounded probe passes, 503 when it fails, 404 for an unknown or
  disabled component, with an empty body publicly.
- `version` is the release version from the configured image tag, keeping a leading `v`
  where the tag has one (a variant suffix such as `-alpine` is dropped), null when the tag is
  not a recognized release. Consumers label it "configured", never "running". `image` is the
  configured reference without digest.
- An absent or unreachable producer means unknown, never unhealthy, and never blocks another
  stack's card or the console.
- Edge proxies each stack's document and health paths same-origin at
  `/stack-status/<stack>` and `/stack-status/<stack>/health/<component>`: GET and HEAD only,
  request credentials and cookies stripped, four-second deadline, 64 KiB and 32-component
  limits, `Cache-Control: no-store`, no upstream error body.
- Accepted loss versus version 1: worker, task and restart states and observed image
  digests are no longer reported (red team finding 7, Owner decision D6).

## Per-repo verification gates

Every PR runs its repo's gates before review and again before merge; the reviewer re-runs
them unshimmed. A failed or skipped gate is reported as such, never as a pass.

| Repo | Commands |
| --- | --- |
| platform-edge | `scripts/validate.sh`; `python3 -m unittest discover -s tests`; `node --test tests/status*.test.cjs`; `scripts/smoke.sh` when `compose.yaml`, the image pin, `Caddyfile`, `routes.d/` or `scripts/bootstrap.py` change |
| llm-gateway-stack | `scripts/validate.sh`; `python3 -m unittest discover -s tests`; `scripts/smoke.sh` when Compose, an image pin, the Caddyfile or bootstrap change |
| observability-stack | `scripts/validate.sh`; `python3 -m unittest discover -s tests`; `scripts/smoke.sh` when Compose, an image pin, the Caddyfile, Alloy config or bootstrap change |
| agent-backplane | After `bun install --frozen-lockfile`: `bun run check`; `python3 -m unittest discover -s tests`; `bun run test` (the `bun test` preload wrapper); `bun tests/acceptance/storage-startup.ts`; `bun tests/acceptance/storage-identity.ts`; `bun tests/acceptance/storage-migration.ts`; `python3 scripts/backup-drill.py --offline`; `python3 scripts/backup-drill.py --s3`; `python3 scripts/storage-migration-drill.py`. When the server or compute image changes, also `docker build -f infra/compose/server.Dockerfile .`, the workerd image build, `bun tests/acceptance/workerd-image.ts <image> --lifecycle` and `python3 tests/acceptance/workerd-gate.py <image>`. A brief may narrow this list to the gates its change can affect; CI runs all of them. |

## Bootstrap contract

One command from clone to running stack. It locks the env file, generates missing secrets
(mode 0600, never rewrites a present value, keeps unmanaged lines byte for byte), refuses to
start when installation state exists and secrets are missing, creates or validates the
Platform Network, runs `docker compose up --wait`, probes readiness, and prints the next step
as JSON. Errors are one JSON line on stderr. Exit codes: 0 ready, 1 refused, 2 usage, 3 not
ready. `--dry-run` and `--env-file` are accepted everywhere (see "Platform contract").
A fresh clone needs no edits before the first run: Observability starts `degraded` with a
placeholder alert contact point when no delivery is configured, and Gateway in `local`
mode records a default Langfuse login with a generated password.

## Network and ingress

- Each stack ships its own Caddy for HTTP/HTTPS. Backplane also keeps a direct loopback
  API port, with Caddy optional. Datastores stay private.
- One public domain per stack with fixed application subdomains. Fresh standalone Caddy
  installations default to `*_ACCESS_MODE=local`: HTTP and internal-CA HTTPS on loopback,
  without redirecting HTTP or telling browsers to require HTTPS. For direct HTTPS, install
  the stack's public CA root on client devices. `public` uses automatically renewed trusted
  certificates and HTTP redirects; choose the external bind address explicitly. `proxy`
  means another gateway handles HTTPS and forwards HTTP to this stack. Application browser
  URLs are configured separately. IP or alternate-host console access does not imply
  arbitrary application aliases.
- Each application has one configured browser URL for authentication and generated links.
  Bootstrap derives defaults from the mode. Backplane core-only remains directly accessible
  over HTTP; its standalone Caddy is optional. A stack behind Platform Edge does not publish
  an unused HTTPS port or share CA private keys.
- Tailscale is optional. Platform Edge documents the shared-host design; each sibling
  README carries a standalone Tailscale recipe.
- On a shared host every stack joins the external Docker network `platform` with only its
  ingress target and metrics endpoints, under prefixed aliases (`lg-`, `bp-`, `ob-`, `pe-`).
  Datastores never join. The optional `platform-edge` project owns 80 and 443 and routes each
  hostname to a stack over that network.

## Data and operations

- Datastore state is Compose volumes, except a Postgres host path where the operator chooses
  the disk. Volumes are named by role.
- A Checkpoint is one consistent backup set across every store plus a manifest of pins and
  configuration, taken with ingestion fenced. `backup.sh`, `restore.sh`, and a drill script
  that proves the pair. RPO and RTO are stated in `docs/operations/backup.md`. Encryption and
  off-host replication of the backup mount are the operator's job.
- Runtime logs go to stdout/stderr. Linux deployments use Docker journald with its extra
  log cache disabled; no application-managed or Docker JSON runtime log files. The host
  controls journal persistence and retention. Non-journald hosts need an explicit supported
  logging override. Caddy access logs are structured JSON with credential redaction.
- Observability is optional: Alloy discovers container logs through Docker and scrapes the
  metrics interfaces above over the Platform Network. Collection failures never prevent
  another stack from starting. Domain data (LLM traces, backplane audit), WAL, backups and
  protected one-off recovery diagnostics remain durable where specified.
- Health: each stack exposes `/health/<component>` for the console and bootstrap, through
  its Caddy (Backplane: its server). Use container healthchecks where supported.
  Observability's Caddy healthcheck probes Caddy only; its distroless backends are probed by
  bootstrap and smoke HTTP assertions through Caddy. Component probes return only a status
  with an empty body to every caller; Observability's `/health/alerts` is a JSON delivery
  state, not a component probe. Gateway and Observability gate no health path by client
  address. Backplane's readiness detail stays on its loopback port behind
  `BP_OPERATIONS_TOKEN`.
- Versions bump through Renovate PRs; majors of stateful stores are labelled and applied only
  from a Checkpoint following `docs/operations/maintenance.md`.

## Agents

Model routing, roles and brief templates live in each repo's `docs/agents/model-routing.md`.

## Stack-specific operations

Observability connects Caddy, Grafana and Alloy to the Platform Network. Its optional
`compose.s3.yaml` replaces backend storage mounts; bootstrap records the selection in
`COMPOSE_FILE`. Platform Edge owns shared ingress and certificate volumes; it has one
Caddy service and no datastore. Per-stack runbooks define their exact health routes,
checkpoint contents and recovery procedures.
