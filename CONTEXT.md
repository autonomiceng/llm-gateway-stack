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
health, and the pinned versions. It has no login and no write actions.
_Avoid_: Dashboard, admin UI, launchpad

**Local Mode**:
The default access mode: plain HTTP on the loopback interface with `*.localhost` hostnames,
no DNS or certificates required, not reachable from other hosts.
_Avoid_: Development mode, insecure mode

**Public Mode**:
The access mode an operator enables by setting a public domain, HTTPS, and a public bind
address. The Stack Gateway obtains certificates automatically. Application and datastore
ports stay private.
_Avoid_: TLS Mode, production mode

**Pinned Version**:
An image reference in `compose.yaml` written as tag plus digest. It is the newest stable
release of that component that has passed the smoke contract.
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
The Docker network named `platform` shared by the four repos on one host (see `docs/conventions.md`). Only ingress
targets and metrics endpoints join it, under stack-prefixed aliases.
_Avoid_: Default network, bridge, mesh

**Experience Change**:
A release change that intentionally alters operator- or caller-visible behavior even when
service interfaces stay compatible. Release notes name it and its rollback.
_Avoid_: Internal change, transparent upgrade, breaking change
