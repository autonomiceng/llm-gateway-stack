# Runtime logs and persistent data

Where runtime logs go, what stays on disk on purpose, and the override for hosts without
journald.

- [Application audit](#application-audit)
- [Intentional persistent state](#intentional-persistent-state)
- [Hosts without journald](#hosts-without-journald)

The deployment default is Docker's `journald` driver for every service, with
`cache-disabled: "true"`. It requires a Linux Docker host with journald available.
There is no remote logging driver and no dependency on Alloy, Loki or the observability
stack. Host journal persistence, retention and quotas remain host policy; these scripts
never change them. Journald supports `docker compose logs` and `journalctl`.

Caddy emits JSON access logs on stdout and runtime logs on stderr. Both encoders remove
request and response headers and redact the entire query string, covering authorization,
cookies, API-key headers and S3 presigned signatures. Paths and status remain searchable.
Never put credentials in URL paths. Request/response bodies are not access-log fields.
Application logs are independent of Caddy's filters; keep diagnostic verbosity and
sensitive payload logging disabled.

The optional observability stack already discovers these containers and reads their logs
through Docker’s API using Alloy’s `loki.source.docker`. Docker reads journald directly,
even with its extra file cache disabled; no additional journal mount is needed. In Loki,
query `{compose_project="llm-gateway-stack"}`, adjusting the project name if changed.
Collection failure or absence does not affect gateway startup or request handling.

## Application audit

| Service | Continuous runtime log destination |
| --- | --- |
| Caddy | stdout access JSON, stderr runtime JSON; no file writer |
| LiteLLM | process output; no file log option or file callback configured |
| Langfuse web/worker | process output; no local file sink configured |
| PostgreSQL | upstream stderr default; logging collector remains off |
| Valkey | upstream process output default; no `logfile` configured |
| ClickHouse | console enabled; `logger.log`, `logger.errorlog` and duplicate `text_log` removed |
| RustFS | image's `/logs` destination overridden with an empty `RUSTFS_OBS_LOG_DIRECTORY`; stdout enabled |
| Exporters and bucket initializer | process output |

The `clickhouse-logs` volume interface is retained but receives no new runtime logs.
Existing contents are never deleted automatically. The Smoke Contract checks that a
fresh ClickHouse and RustFS startup creates no nonempty files in their log directories.
Repeat this audit when changing image pins.

## Intentional persistent state

Langfuse traces, prompts, evaluations, media and raw ingestion events are product data.
LiteLLM keys, budgets, spend records and model settings are product data. PostgreSQL WAL,
Valkey AOF, database audit/query history, Checkpoints and protected one-off backup
diagnostics support recovery or auditing. Caddy certificate and configuration volumes
hold TLS state. These retain their existing storage and backup policies.

ClickHouse's structured query/audit and metric tables retain upstream behavior; the
continuous server text-log duplicate is disabled. Langfuse owns its application schema.
No local DDL, table cleanup or volume deletion accompanies this logging change.
Loki storage, when an external observability stack is installed, is that stack's product
state and retention policy.

## Verification and troubleshooting

`docker compose logs --tail=20 caddy` must print JSON lines, and
`journalctl CONTAINER_NAME=llm-gateway-stack-caddy-1 -n 20` must show the same lines.
Access log entries must carry no headers and a `?REDACTED` query string.

| Symptom | Cause and fix |
| --- | --- |
| `docker compose up` fails with `journald` driver errors | The host has no journald (non-systemd host, or Docker in a container). Use the override below. |
| `docker compose logs` prints nothing | The journal is not persistent or its retention dropped the entries; host policy, not the stack. |
| Loki shows no `compose_project="llm-gateway-stack"` stream | Alloy is not running or has no Docker discovery on this host; the stack does not depend on it. |

## Hosts without journald

For a deliberate no-runtime-logs deployment, add an untracked Compose override and append
its path to the end of the `COMPOSE_FILE` bootstrap recorded; bootstrap keeps it there. Docker's `none` driver is portable and
creates no container log files, but loses `docker logs` and all runtime diagnostics:

```yaml
x-no-logs: &no-logs
  logging: !override
    driver: none
services:
  caddy: *no-logs
  litellm: *no-logs
  langfuse-web: *no-logs
  langfuse-worker: *no-logs
  clickhouse: *no-logs
  rustfs: *no-logs
  rustfs-init: *no-logs
  valkey: *no-logs
  postgres: *no-logs
  valkey-exporter: *no-logs
  postgres-exporter: *no-logs
```

Recreate containers to apply a logging-driver change. A local/json-file driver is an
operator departure from the no-runtime-files policy and requires explicit rotation.
The repository's validation and smoke gates exercise the supported journald default.

References: [Docker journald](https://docs.docker.com/engine/logging/drivers/journald/),
[Docker file cache](https://docs.docker.com/engine/logging/dual-logging/),
[Caddy log filters](https://caddyserver.com/docs/caddyfile/directives/log),
[RustFS entrypoint](https://github.com/rustfs/rustfs/blob/1.0.0/entrypoint.sh),
[ClickHouse logger configuration](https://github.com/ClickHouse/ClickHouse/blob/master/programs/server/config.xml),
[Alloy Docker source](https://grafana.com/docs/alloy/latest/reference/components/loki/loki.source.docker/).
