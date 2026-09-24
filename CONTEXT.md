# LLM Gateway Stack

The terms this repository uses, one sentence each. Use these words in code, docs and
commits; avoid the listed alternatives.

**Reference Stack**: a production-capable single-host deployment with secure defaults,
durable local data and tested backup, restore and upgrade procedures, without high
availability, zero-downtime upgrades or failover.
_Avoid_: production distribution, demo stack, highly available stack

**Stack Gateway**: the Caddy instance that is the only published network entry, routing
by hostname to every application and serving the Stack Console.
_Avoid_: landing page, service router, public port set

**Stack Console**: the page the Stack Gateway serves at the root hostname, with links to
every application, live health and the configured versions from the Status Document, with
no login and no write actions.
_Avoid_: dashboard, admin UI, launchpad

**Status Document**: the public Status v2 file bootstrap writes after readiness and the
Stack Gateway serves at `/status.json`, recording each component's configured image,
version, profile state and health path, never observed runtime state.
_Avoid_: status observation, versions file

**Local Mode**: the default access mode, serving HTTP and internal-CA HTTPS on loopback
without redirects or HSTS, so a clean clone starts without edits.
_Avoid_: development mode, insecure mode

**Public Mode**: the access mode for public DNS hostnames, with HTTPS redirects and
certificates from ACME by default.
_Avoid_: TLS mode, production mode

**Proxy Mode**: the access mode behind Platform Edge or another gateway, listening on HTTP
only and trusting the configured proxies while application URLs keep their external scheme.
_Avoid_: TLS passthrough, shared certificates

**Issuer**: the source of the Stack Gateway's HTTPS certificates, selected by
`LG_TLS_ISSUER` independently of the access mode: Caddy's internal CA, an ACME directory,
or operator certificate files.
_Avoid_: certificate mode, TLS provider

**Pinned Version**: the default image reference in `compose.yaml`, written as tag plus
digest inside an `LG_*_IMAGE` fallback, being the newest stable release that passed the
Smoke Contract.
_Avoid_: latest, floating tag, release manifest

**Smoke Contract**: the executable check (`scripts/smoke.sh`) that a fresh install of the
Pinned Versions is usable: every service healthy, a completion through the gateway, its
trace in Langfuse, metrics exposed, object storage round-trips, the queue surviving a restart.
_Avoid_: validation ladder, rehearsal, CI

**Checkpoint**: one consistent backup set across Postgres, ClickHouse, object storage and
configuration, taken with ingestion fenced, and the unit of restore and rollback.
_Avoid_: recovery point, snapshot, dump

**Platform Network**: the external Docker network `platform` shared by the four stacks on
one host, with the fixed allocation `172.30.0.0/24` and Platform Edge at `172.30.0.2`,
joined only by ingress targets and metrics endpoints under stack-prefixed aliases.
_Avoid_: default network, bridge, mesh

**Experience Change**: a release change that intentionally alters what an operator or a
caller sees even when interfaces stay compatible, named with its rollback in the release notes.
_Avoid_: internal change, transparent upgrade, breaking change
