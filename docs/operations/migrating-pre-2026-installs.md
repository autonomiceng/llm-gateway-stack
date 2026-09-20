# Migrating a pre-2026 installation

This stack does not upgrade in place from the previous generation (the release-framework era with MinIO, Redis 7, Postgres 17, Langfuse 3 and the `NGINX_PORT` and `versions.env` conventions). Migration is a planned maintenance window run by an operator or a dedicated agent. This page is the handoff: what to collect, what changes, which steps are one-way, and how to know it worked.

## Collect first, on the old host, read-only

```sh
git -C <old checkout> rev-parse HEAD
docker compose ps --format '{{.Service}} {{.Image}} {{.Status}}'
docker compose config | grep -E 'image:|volumes:|- .*:/'      # no secrets in this output
docker volume ls; docker system df -v | head -40
cat <postgres data path>/PG_VERSION
grep -E '^[A-Z_]+=' .env | cut -d= -f1                          # keys only
docker compose exec clickhouse clickhouse-client -q 'SELECT version()'
docker compose exec langfuse-web wget -qO- http://127.0.0.1:3000/api/public/health
ls -la <backup location>; date of last restore test
```

Also record: which hostnames or ports external clients use, which virtual keys exist in LiteLLM, and whether Langfuse retention is configured.

## What changes

| Old | New |
| --- | --- |
| `NGINX_PORT`, `GATEWAY_PORT`, `PUBLIC_BIND_IP`, `LOCAL_BIND_IP` | `LG_BIND_HOST`, `LG_HTTP_PORT`, `LG_HTTPS_PORT` |
| `*_HOSTNAME` per service, TLS overlay file | `LG_PUBLIC_DOMAIN`, `LG_ACCESS_MODE`, optional browser URL protocol `LG_SCHEME` |
| `POSTGRES_DATA_HOST_PATH` mounted at `/var/lib/postgresql/data` | `LG_POSTGRES_DATA_DIR` mounted at `/var/lib/postgresql` (Postgres 18 layout) |
| `REDIS_AUTH`, service `redis`, volume `langfuse_redis_data` | `VALKEY_PASSWORD`, service `valkey`, volume `valkey-data` |
| `MINIO_ROOT_USER/PASSWORD`, service `minio`, volume `langfuse_minio_data` | `RUSTFS_ACCESS_KEY/SECRET_KEY`, service `rustfs`, volume `rustfs-data` |
| `LITELLM_DB_USER=llmproxy` | role `litellm` |
| `versions.env`, `.versions.override.env`, `./stack` | pins in `compose.yaml`; no CLI |
| Prometheus service | removed; metrics scraped by the observability stack |
| LiteLLM `langfuse` callback | `langfuse_otel` |
| Direct application ports, `/litellm/` and `/langfuse/` redirects | hostnames only |

Volume names change, so nothing is reused by accident. Every store is copied, not remounted.

## One-way steps, in order

1. **Checkpoint the old stack** with its own backup procedure and verify a restore on a scratch host. Nothing below starts without this.
2. **Postgres 17 to 18.** Stop the old stack. Use the copied-cluster command sequence below, with a digest-pinned one-shot helper and a single parent mount at `/var/lib/postgresql`. Rename role `llmproxy` to `litellm` and reset both application passwords to the values in the new `.env`.
3. **ClickHouse.** The old pin is 26.3; the new one is 26.8. Copy the volume to `rustfs`-era naming (`<LG_VOLUME_PREFIX>-clickhouse-data`) and start it with Langfuse stopped. Langfuse's migrations run on first start of the new version.
4. **Objects: MinIO to RustFS.** Start RustFS with the new credentials, create the `langfuse` bucket, copy every object with `aws s3 sync` between the two endpoints, then compare object counts and a sample of checksums. Keep the MinIO volume until the new stack has run a week.
5. **Langfuse 3 to 4.** Start the new Langfuse against the copied stores with `LANGFUSE_MIGRATION_V4_WRITE_MODE=dual` in `.env` (compose.yaml passes it to web and worker), let the historic backfill finish (watch the worker log; it needs about three times the ClickHouse disk of the events table), then switch to `events_only` and remove the variable. After the switch, v3 clients and the legacy ingestion API stop working. Rollback before the switch is restore of the Checkpoint plus the old images; after the switch it is restore only.
6. **Queue.** Drain the old Redis before step 5 by stopping LiteLLM and Langfuse web and waiting for the worker to idle. The new Valkey starts empty.
7. **LiteLLM.** Point the new stack at the migrated `litellm` database. Virtual keys, teams and spend survive; the callback changes to `langfuse_otel`, so traces from old and new requests look different in Langfuse.
8. **Cut over ingress.** Update DNS or client configuration from ports and paths to hostnames. The old gateway's redirect paths do not exist in the new one.

## Acceptance

`scripts/smoke.sh` boots a disposable project and cannot inspect the migrated installation. Run its probes by hand against the migrated stack (a mock completion, the observations query, `/metrics` from inside the network, the RustFS object listing), plus: a virtual key that existed before still works with its budget intact; a trace from before the migration opens in Langfuse with its media; a batch export downloads; a restore drill from the first new Checkpoint succeeds on a scratch host.

## PostgreSQL 17 to 18 command sequence

Rehearsal status: **pending on the host**. Docker registry and daemon access were denied
in the implementation sandbox. Do not treat these commands as a completed rehearsal.
The orchestrator must fill the digest TODO, run on a copied cluster, and record the exit
status and application checks before production use. Extensions must be available in the
helper and final image; custom tablespaces need a separate migration procedure.

TODO: replace `PGAUTO_DIGEST_TODO` below with the output of this host command:

```sh
docker buildx imagetools inspect pgautoupgrade/pgautoupgrade:18-bookworm --format '{{.Manifest.Digest}}'
```

Run in a maintenance shell from the new checkout. Set absolute paths to the old checkout,
its PG17 data directory (the directory containing `PG_VERSION`), and a new empty parent
for PG18. The physical copy must finish with the old stack stopped. Use a real copy or
copy-on-write reflink, never hard links to the rollback source.

```sh
set -eu
old_checkout=/opt/gateway-old
old_pg=/srv/gateway-old/postgres
new_pg=/srv/gateway/postgres18
upgrade_image='pgautoupgrade/pgautoupgrade:18-bookworm@PGAUTO_DIGEST_TODO'
case "$upgrade_image" in *@sha256:*) ;; *) echo 'Fill the digest TODO first' >&2; exit 1;; esac
test "$(sudo cat "$old_pg/PG_VERSION")" = 17
test ! -e "$new_pg"
# Take and verify the old stack's whole-stack Checkpoint before stopping it.
docker compose -f "$old_checkout/compose.yaml" --project-directory "$old_checkout" down
sudo mkdir -p "$new_pg/17/docker"
sudo cp -a --reflink=auto "$old_pg/." "$new_pg/17/docker/"
sudo sync -f "$new_pg"
docker run --rm --network none --user 0 \
  -e PGAUTO_ONESHOT=yes \
  --mount "type=bind,src=$new_pg,dst=/var/lib/postgresql" "$upgrade_image"
test "$(sudo cat "$new_pg/18/docker/PG_VERSION")" = 18
# Set LG_POSTGRES_DATA_DIR to new_pg in the NEW .env; preserve its other secrets.
export LG_POSTGRES_DATA_DIR="$new_pg"
# Create the backup mount and external volumes via the documented migration steps first.
docker compose up -d --no-deps --wait postgres
docker compose exec -T postgres psql -U postgres -d postgres -v ON_ERROR_STOP=1 \
  -c 'SELECT version();' -c 'SELECT datname FROM pg_database ORDER BY datname;'
docker compose exec -T postgres sh -ec '
  psql -U postgres -d postgres -v ON_ERROR_STOP=1 \
    --set=litellm_password="$LITELLM_DB_PASSWORD" \
    --set=langfuse_password="$LANGFUSE_DB_PASSWORD"
' <<'SQL'
ALTER ROLE llmproxy RENAME TO litellm;
SELECT format('ALTER ROLE litellm PASSWORD %L', :'litellm_password')\gexec
SELECT format('ALTER ROLE langfuse PASSWORD %L', :'langfuse_password')\gexec
SQL
docker compose exec -T postgres bash /docker-entrypoint-initdb.d/02-monitor.sh
docker compose exec -T postgres vacuumdb -U postgres --all --analyze-in-stages
```

Confirm the old installation's actual role names before the rename, and compare database
counts and key/spend rows against the stopped source. Complete the remaining store and
Langfuse migration steps before application startup and ingress cutover.

Rollback before new application writes keeps the untouched PG17 source and old volumes:

```sh
docker compose down
unset LG_POSTGRES_DATA_DIR
docker compose -f "$old_checkout/compose.yaml" --project-directory "$old_checkout" up -d --wait
```

Keep the failed/copied PG18 parent for diagnosis. Never mount the upgraded cluster into
PG17. After new application writes, rollback requires whole-stack Checkpoint restore and
an explicit decision about losing those later writes. The one-parent-mount layout follows
[pgautoupgrade's PostgreSQL 18 instructions](https://github.com/pgautoupgrade/docker-pgautoupgrade#error-message-when-mounting-data-to-varlibpostgresqldata-on-postgres-v18).
