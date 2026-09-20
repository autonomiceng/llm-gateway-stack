# llm-gateway-stack

One URL for every model, one key per app or agent, and a trace for every call. Self-hosted, one Docker Compose project.

[![CI](https://github.com/autonomiceng/llm-gateway-stack/actions/workflows/ci.yml/badge.svg)](https://github.com/autonomiceng/llm-gateway-stack/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)
[![LiteLLM 1.101](https://img.shields.io/badge/LiteLLM-1.101-informational)](https://github.com/BerriAI/litellm)
[![Langfuse 4.37](https://img.shields.io/badge/Langfuse-4.37-informational)](https://github.com/langfuse/langfuse)
[![PostgreSQL 18](https://img.shields.io/badge/PostgreSQL-18-4169E1)](https://www.postgresql.org)

## What it is

You have agents, scripts and internal tools that all need an LLM. Handing each one a provider key means no budgets, no idea who spent what, and a painful rotation. This stack puts [LiteLLM](https://github.com/BerriAI/litellm) in front of your providers and [Langfuse](https://github.com/langfuse/langfuse) behind it.

You give each consumer its own virtual key, and you can put a budget and a rate limit on it. Every request produces a trace with the prompt, the response, tokens, cost and latency, and you can open it when something looks wrong. Traces stay on your host; the request itself still goes to whichever model provider you configured.

It runs on one machine and is built to stay up: pinned images, backups with a restore drill, a smoke test that gates every version bump, and a written maintenance procedure.

## Quick start

You need a Linux Docker host with journald, Compose 2.24.4 or newer, Python 3.11 or newer, and about 6 GB of disk for images. [mise](https://mise.jdx.dev) installs the pinned tools if you use it. See [logging](docs/operations/logging.md) for hosts without journald.

```sh
git clone https://github.com/autonomiceng/llm-gateway-stack.git
cd llm-gateway-stack
cp .env.example .env
# Set LANGFUSE_INIT_USER_EMAIL and LG_BACKUP_DIR in .env first.
# The backup directory must exist on a filesystem separate from Postgres data.
python3 scripts/bootstrap.py
```

Bootstrap writes `.env` with generated secrets, creates the shared `platform` network, starts everything, waits for it to be healthy and prints the links. About a minute.

| URL | What |
| --- | --- |
| `http://localhost/` | Console: links, live health; pinned versions for operators |
| `http://litellm.localhost/` | The gateway API; `/ui/` restricted to operators |
| `http://langfuse.localhost/` | Traces, evals, prompts |
| `http://rustfs.localhost/` | RustFS admin console; operator access and RustFS login required |

Try it without any provider keys:

```sh
source <(grep LITELLM_MASTER_KEY .env)
curl -s http://litellm.localhost/v1/chat/completions \
  -H "Authorization: Bearer $LITELLM_MASTER_KEY" -H 'Content-Type: application/json' \
  -d '{"model":"gateway-mock","messages":[{"role":"user","content":"hello"}]}'
```

Log in to Langfuse with `LANGFUSE_INIT_USER_EMAIL` and `LANGFUSE_INIT_USER_PASSWORD` from `.env`. The trace is there. Add real models in `config.yaml`, put their keys in `.env`, and run `docker compose up -d litellm`. The master key has no budget; before handing out access, create per-consumer keys with limits as shown in [keys and budgets](docs/operations/keys.md).

Local Mode serves HTTP and self-signed HTTPS without redirecting HTTP or telling browsers to require HTTPS. To put it on the internet, set `LG_ACCESS_MODE=public`, a domain and a public bind address in `.env`. Choose `LG_ACCESS_MODE=proxy` when Platform Edge or another gateway handles HTTPS. Details in [ingress](docs/operations/ingress.md). Linux journald receives runtime logs; optional Alloy collection and portability are covered in [logging](docs/operations/logging.md).

## What's inside

| Service | Job | Data |
| --- | --- | --- |
| Caddy | The only published port. Routes by hostname, serves the console. | volume |
| LiteLLM | Keys, budgets, routing, caching, `/metrics` | PostgreSQL, Valkey |
| Langfuse web and worker | Traces, evals, prompts, cost | PostgreSQL, ClickHouse, RustFS, Valkey |
| PostgreSQL 18 | Keys, spend, users, projects | host path you choose |
| ClickHouse | Trace analytics | volume |
| RustFS | S3-compatible store for events, media, exports | volume |
| Valkey | Ingestion queue and cache, `noeviction`, AOF | volume |

Every image is pinned as `tag@sha256` in `compose.yaml`. Renovate opens the bump; a human merges it after the smoke test passes.

## Built on

| Project | Stars | What we use it for |
| --- | --- | --- |
| [LiteLLM](https://github.com/BerriAI/litellm) | ![stars](https://img.shields.io/github/stars/BerriAI/litellm?style=flat) | OpenAI-compatible proxy, virtual keys, budgets, routing |
| [Langfuse](https://github.com/langfuse/langfuse) | ![stars](https://img.shields.io/github/stars/langfuse/langfuse?style=flat) | Tracing, evals, prompt management |
| [Caddy](https://github.com/caddyserver/caddy) | ![stars](https://img.shields.io/github/stars/caddyserver/caddy?style=flat) | Ingress and automatic HTTPS |
| [PostgreSQL](https://github.com/postgres/postgres) | ![stars](https://img.shields.io/github/stars/postgres/postgres?style=flat) | Relational state for both apps |
| [ClickHouse](https://github.com/ClickHouse/ClickHouse) | ![stars](https://img.shields.io/github/stars/ClickHouse/ClickHouse?style=flat) | Trace analytics for Langfuse |
| [RustFS](https://github.com/rustfs/rustfs) | ![stars](https://img.shields.io/github/stars/rustfs/rustfs?style=flat) | S3-compatible object storage |
| [Valkey](https://github.com/valkey-io/valkey) | ![stars](https://img.shields.io/github/stars/valkey-io/valkey?style=flat) | Queue and cache |
| [Docker Compose](https://github.com/docker/compose) | ![stars](https://img.shields.io/github/stars/docker/compose?style=flat) | Running it all |

## The other stacks

This is one of four repos that deploy the same way and work together on one host:

- [agent-backplane](https://github.com/autonomiceng/agent-backplane): shared state, queues and approvals for agents.
- [observability-stack](https://github.com/autonomiceng/observability-stack): Grafana, Loki, Tempo and Mimir. Collects this stack's logs and metrics over the `platform` network.
- [platform-edge](https://github.com/autonomiceng/platform-edge): one Caddy for ports 80 and 443 when more than one stack shares a host.

Each runs alone. Shared conventions are in [docs/conventions.md](docs/conventions.md).

## Day two

- [Ingress and access modes](docs/operations/ingress.md)
- [Backup, restore and the monthly drill](docs/operations/backup.md)
- [Maintenance and version bumps](docs/operations/maintenance.md)
- [Host sizing](docs/operations/capacity.md)
- [Migrating a pre-2026 install](docs/operations/migrating-pre-2026-installs.md)
- [Design](docs/DESIGN.md), [vocabulary](CONTEXT.md), [decisions](docs/adr/)

## Development

```sh
scripts/validate.sh                    # static checks, what CI runs on every push
python3 -m unittest discover -s tests  # unit tests, no Docker
scripts/smoke.sh                       # boots a disposable copy and proves it works
scripts/backup-drill.sh                # backup, wipe, restore, verify
```

CI runs the first two on every push and the smoke test on every PR, weekly, and on demand. See [CONTRIBUTING.md](CONTRIBUTING.md).

## Security

Report vulnerabilities through the [security policy](SECURITY.md). Nothing listens outside loopback until you say so.

## License

[MIT](LICENSE).

**Third-party components.** LiteLLM and Langfuse include MIT-licensed code and enterprise-gated code inside their images. Enterprise features require the respective vendor's license. The repository license does not grant rights to those features.
