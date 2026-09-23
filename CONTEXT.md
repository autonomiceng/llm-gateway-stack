# LLM Gateway Stack

This glossary defines the operational language for the self-hosted LiteLLM and Langfuse
stack and the promises it makes to operators.

## Language

**Reference Stack**:
A production-capable, single-host deployment with secure defaults, durable local data, and
tested backup, restore, and upgrade procedures. It does not promise high availability,
zero-downtime upgrades, automated failover, or SLA-backed support.
_Avoid_: Production distribution, demo stack, highly available stack

**Stack Gateway**:
The Caddy instance that is the only published network entry to the Reference Stack. It
routes by hostname to every application and serves the Stack Console.
_Avoid_: Landing page, service router, public port set

**Stack Console**:
The page the Stack Gateway serves at the root hostname: links to every application, live
health, and the configured image versions from the Status Document. It has no login and
no write actions.
_Avoid_: Dashboard, admin UI, launchpad

**Status Document**:
The public Status v2 file bootstrap writes after readiness and the Stack Gateway serves at
`/status.json` in every access mode: each component's configured image and version, whether
the selected profiles enable it, its health path, application origins, and the newest
Checkpoint time at that bootstrap. It records configuration, never observed runtime state.
_Avoid_: Status observation, versions file

**Local Mode**:
The default access mode: HTTP and HTTPS on loopback, without redirects or HSTS. HTTPS uses
the internal CA unless another Issuer is selected.
Application origins use HTTP by default and keep explicit hostnames.
_Avoid_: Development mode, insecure mode

**Public Mode**:
The access mode for public DNS hostnames with HTTPS redirects. Certificates come from ACME
by default. Application and datastore ports stay private.
_Avoid_: TLS Mode, production mode

**Issuer**:
The source of the Stack Gateway's HTTPS certificates, selected by `LG_TLS_ISSUER`
independently of the access mode: Caddy's internal CA, an ACME directory, or operator
certificate files.
_Avoid_: Certificate mode, TLS provider

**Proxy Mode**:
The access mode behind Platform Edge: the Stack Gateway listens on HTTP and trusts only
configured proxies, while application origins retain their configured external scheme.
_Avoid_: TLS passthrough, shared certificates

**Pinned Version**:
The default image reference in `compose.yaml`, written as tag plus digest inside an
`LG_*_IMAGE` fallback. It is the newest stable release of that component that has passed
the smoke contract. Operator image overrides are separate from the Pinned Version.
_Avoid_: Latest, floating tag, release manifest

**Smoke Contract**:
The executable check that a fresh install of the pinned versions is usable: every service
healthy, a completion through the gateway, its trace visible in Langfuse, metrics exposed,
object storage round-trips, and the queue surviving a restart. Passing it is what makes a
version a Pinned Version.
_Avoid_: Validation ladder, rehearsal, CI

**Checkpoint**:
One consistent backup set across Postgres, ClickHouse, object storage and configuration,
taken with ingestion paused. The unit of restore and the rollback boundary before a
persistent change.
_Avoid_: Recovery point, snapshot, dump

**Platform Network**:
The Docker network named `platform` shared by the four repos on one host (see `docs/conventions.md`), with the
fixed allocation `172.30.0.0/24` and Platform Edge at the reserved address `172.30.0.2`. Only ingress
targets and metrics endpoints join it, under stack-prefixed aliases.
_Avoid_: Default network, bridge, mesh

**Experience Change**:
A release change that intentionally alters operator- or caller-visible behavior even when
service interfaces stay compatible. Release notes name it and its rollback.
_Avoid_: Internal change, transparent upgrade, breaking change
