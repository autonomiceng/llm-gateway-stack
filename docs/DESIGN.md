# llm-gateway-stack: systems design

One host, one Compose project: an OpenAI-compatible gateway with keys, budgets and routing in
front of any provider, and full tracing of every call, for people and for agents.

Status: accepted 2026-09-17. Decisions live in adr/. Vocabulary lives in `../CONTEXT.md`. This document is the map.

## Why it exists

Every agent harness and every internal tool wants an LLM endpoint. Handing each one a
provider key means no budgets, no per-caller attribution, no place to look when something
goes wrong, and a key rotation that touches every consumer. The gateway gives them one URL,
one virtual key each, and a trace for every request. Self-hosted, because prompts and
responses are the most sensitive data most teams have.

## Guarantees, stated exactly

- Every request through the gateway is authenticated by a LiteLLM key and produces a
  Langfuse trace with cost, latency and the caller's identity. Budgets and rate limits are
  per key and are set when the key is created; the master key has none. The operator
  creates consumer keys before handing out access (docs/operations/keys.md).
- A fresh install on a clean host is one command and takes minutes, with no provider
  credentials required to prove it works.
- A Checkpoint restores the whole stack to a consistent point: traces, media, keys, spend.
  RPO is the Checkpoint interval; the WAL archive supports manual point-in-time recovery
  by an expert. Daily successful off-host Checkpoints target twenty-four hours (ADR-0002).
- The only network entry is Caddy. In Local Mode nothing leaves the loopback interface.
- Response reuse never happens unless the caller asks for it (ADR-0011).

Not promised: high availability, zero-downtime upgrades, automated failover, in-place
upgrades from the pre-2026 stack (ADR-0001, ADR-0014).

## Shape

```
  Agents / apps           Browser
      │ OpenAI API            │ console, admin UIs
      └───────────┬───────────┘
        ┌─────────▼──────────┐   :80 / :443 on LG_BIND_HOST
        │  caddy             │   litellm.<d>  langfuse.<d>  s3.<d>  rustfs.<d>  <d> = console
        └──┬──────────┬──────┘
           │          │
   ┌───────▼───┐  ┌───▼────────────┐        ┌───────────────┐
   │ litellm   │  │ langfuse-web   │        │ langfuse-worker│
   │ :4000     │──│ :3000  (OTLP)  │        │ :3030          │
   └──┬────┬───┘  └───┬───────┬────┘        └──┬─────┬───┬───┘
      │    │          │       │                │     │   │
   ┌──▼────▼──┐  ┌────▼───┐ ┌─▼──────────┐ ┌───▼──┐ ┌▼───▼──────┐
   │ postgres │  │ valkey │ │ clickhouse │ │rustfs│ │ (same)    │
   │ 18       │  │ 9      │ │ 26.8       │ │ 1.0  │ │           │
   └──────────┘  └────────┘ └────────────┘ └──────┘ └───────────┘
      host path     volume      volume        volume

   platform network (external): caddy as lg-gateway, litellm as lg-litellm
```

LiteLLM keeps keys, teams, spend and model config in its Postgres database and uses Valkey
for its operational cache and rate limits. Langfuse keeps users, projects and prompts in its
own Postgres database, traces in ClickHouse, raw events and media in RustFS, and its
ingestion queue in the same Valkey instance. LiteLLM sends spans to Langfuse over OTLP.

## Access modes

`LG_ACCESS_MODE` selects Local, Public or Proxy Mode. Local Mode serves HTTP and
private-CA HTTPS on loopback, without redirects or HSTS. Public Mode uses ACME and
redirects HTTP except root health probes. Proxy Mode listens on HTTP behind Platform
Edge with no published HTTPS port. The template selects the matching small Compose
override; the application and datastore topology stays in `compose.yaml` (ADR-0015).
Application origins are configured independently of the listener scheme and request Host.
See [ingress](operations/ingress.md) for ports, explicit application hostnames and trust.

## Operations

- Public status: `scripts/status_observer.py` publishes `data/console/status.json`;
  periodic observation is opt-in through `scripts/install_status_timer.py`.
  See [status operation](operations/status.md) for public disclosure and probe limits.

- Bootstrap: `scripts/bootstrap.py`. Generates secrets once, refuses to invent secrets over
  existing data, creates the platform network, starts the stack, waits for readiness.
  Newly created external volumes carry the Compose project label so interrupted installs
  can identify them. Existing volumes retain their names, labels and contents.
- Validate: `scripts/validate.sh` for static checks; `scripts/smoke.sh` boots the pinned
  images and proves the Smoke Contract.
- Backup and restore: `scripts/backup.sh`, `scripts/restore.sh`, `docs/operations/backup.md`.
- Upgrades: Renovate proposes, the smoke contract gates, `docs/operations/maintenance.md`
  tells the operator what a major changes and where the rollback boundary is.
- Observability: runtime logs go to the host journal with no Docker file cache.
  Alloy collection and metrics scraping are optional; startup has no observability-stack
  dependency. LLM traces live in Langfuse. See [logging](operations/logging.md).

## Stack

Caddy 2.11, LiteLLM 1.101, Langfuse 4.37, PostgreSQL 18, ClickHouse 26.8 LTS, RustFS 1.0,
Valkey 9.1. Default images are pinned by digest in `compose.yaml`; optional `LG_*_IMAGE` settings
replace complete references through native Compose interpolation. Python 3 standard library for scripts.
No repository build step; prepare operator images separately.

## Explicitly not built

Multi-host or Kubernetes deployment, a second Valkey instance (split queue from cache only
if the memory alert fires), an in-repo metrics store, adoption tooling for older installs.
