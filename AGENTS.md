# Agent guide (AI assistants & automation)

This repository is a **Docker Compose** stack for **LiteLLM** and **Langfuse** with shared Postgres, ClickHouse, MinIO, Redis, and Prometheus.

## Lead contributor

[Randolph Voorhies](https://github.com/randvoorhies) is the lead contributor behind this project.

## Before you change anything

- Read [README.md](README.md) for architecture, pinned images, Postgres host bind mount, Langfuse ClickHouse workaround, and security notes.
- Prefer **minimal diffs**; do not refactor unrelated services or add docs the user did not ask for.
- `docker-compose.yml` uses **environment-variable image pins** (`${SERVICE_IMAGE:-default}`). Defaults live in `.env.example`.

## Local prerequisites

- A file named **`.env`** must exist for `docker compose` (including `docker compose config`) because `litellm` declares `env_file: .env`. Bootstrap: `cp .env.example .env` then `./scripts/init_env.py --force`.
- **LiteLLM** is consumed as a **prebuilt image** only; there is no `Dockerfile` for `litellm` in this repo.
- **Postgres data** is intentionally stored on the host at `POSTGRES_DATA_HOST_PATH` (see `.env.example`).

## Useful commands

```bash
docker compose config
docker compose pull
docker compose up -d
docker compose --profile migrate run --rm migrate-langfuse-events
```

## Conventions

- Secrets: never commit `.env`; template is `.env.example`.
- ClickHouse DDL/backfill: `docker/clickhouse/*.sql` — keep in sync with Langfuse upstream when official migrations appear (README has details).

For human contribution workflow, see [CONTRIBUTING.md](CONTRIBUTING.md).
