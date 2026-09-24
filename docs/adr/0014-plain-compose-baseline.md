---
status: accepted
amended_by: ADR-0015 (access modes and runtime logging)
date: 2026-09-17
---

# Reset to a plain Compose baseline

Supersedes ADR-0003, 0004, 0006, 0008, 0009, 0010 and 0013.

Why: the release framework (Release Manifest, Installation Version State, Custom Pins, a
checkpointed upgrade orchestrator, an adoption rehearsal) was built to trail upstream safely
for one existing installation. It cost 3,000 lines of scripts and tests, its own vocabulary,
and an operator experience nothing else in the industry looks like. Meanwhile the pinned MinIO
image was deleted upstream and a fresh install stopped working. The Owner runs all three
stacks in production and wants them to feel like any other Compose project to an experienced
engineer.

Decision:

- One `compose.yaml` at the repo root. Default images are pinned inline as `${LG_COMPONENT_IMAGE:-image:tag@sha256}`.
  Renovate raises version PRs; a human merges them; nothing deploys automatically.
- Newest stable that passes the smoke contract for default pins. A written maintenance
  procedure replaces the orchestrator: read the notes, take a backup, apply in order, verify,
  and know the rollback boundary for each store.
- Hostnames in every mode. Caddy is the only published entry. `http://litellm.localhost`
  on loopback by default; `https://litellm.<domain>` with ACME when the operator sets a
  domain, scheme and bind address. No path-prefix redirects, no direct application ports.
- RustFS replaces MinIO. Valkey replaces Redis, one instance, service `valkey`, with
  `noeviction`, AOF, a TTL on every LiteLLM key and memory headroom.
- Postgres 18, host path mounted at `/var/lib/postgresql` per the image layout.
- Langfuse 4 in `events_only` mode; LiteLLM reports through `langfuse_otel`.
- Migration of the pre-2026 production installation is a separate effort with its own
  handoff document. This repo does not ship adoption tooling.

Amended 2026-09-20: every service, including helpers, accepts an optional complete image
reference in `.env` through native Compose interpolation. Empty values use the unchanged
inline default; shell precedence and Compose overlays still apply. Operator references may
use mutable or local tags. Langfuse web and worker keep separate references and must use
matching versions. This narrowly replaces the literal-only image policy, without a
release manifest, env-default file, or command wrapper. Checkpoints record immutable
registry identities; images without one are refused before capture or fencing. Restore
requires the recorded immutable references. Default pins alone carry the Smoke Contract.

Consequence: existing installations cannot upgrade in place. Every rename and one-way step
was listed in a migration handoff document. The stack promises fresh install, backup,
restore and forward upgrades between validated pins, and nothing else.

Amended 2026-09-24: the handoff document and the Langfuse v4 migration write-mode setting
are removed; no predecessor installation remains to migrate.
