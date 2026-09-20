# Maintenance

Default versions are pinned by digest in `compose.yaml`. Renovate opens a pull request when an upstream releases; nothing changes on a host until an operator merges and pulls. This page is the procedure for applying a merged bump.

## Image overrides

Copy the relevant empty `LG_*_IMAGE` assignment from `.env.example` into `.env`,
uncomment it, and supply a complete reference such as `registry.example/gateway:trial` or
`registry.example/gateway@sha256:<digest>`. Every service has an override, including the
AWS CLI helper. Langfuse web and worker use separate references and must stay on matching
versions. Empty or unset values use the inline default. Exported values take precedence,
including an empty value selecting the default. Normal `docker compose` commands apply.

Take a Checkpoint before changing a running installation's images. Preserve the previous
settings for rollback; data migration boundaries below also apply to overrides. Restore
requires the exact immutable references in the Checkpoint; see [backup](backup.md).
Local images can run, but need a registry digest before Checkpoint capture. Refresh Stack
Console metadata with bootstrap after applying a change. `validate.sh` checks isolated
repository defaults; it does not certify operator overrides. Validate experiments in a
disposable installation before applying them to persistent data.

Renovate's native Docker Compose manager supports inline defaults, covered by its
[default-variable extraction test](https://github.com/renovatebot/renovate/blob/main/lib/modules/manager/docker-compose/extract.spec.ts).
Langfuse grouping and major-update maintenance labels remain unchanged.

## Before touching a host

1. Read the release notes for every image that changed. Langfuse, Postgres and ClickHouse majors are one-way for data.
2. Run `scripts/smoke.sh` on the commit you are about to deploy. CI runs it on every PR, weekly, and on demand (workflow dispatch); run it where you can see the output.
3. Take a Checkpoint: `scripts/backup.sh`. This is the rollback boundary.

## Applying

```sh
git pull
docker compose pull
docker compose up -d --wait
scripts/validate.sh
curl -fsS http://localhost/health/litellm http://localhost/health/langfuse
```

Compose recreates only the containers whose image or configuration changed. Datastores keep their volumes. Expect a few minutes of gateway downtime while LiteLLM and Langfuse restart; callers see connection errors, not wrong answers.

## Rollback boundary per store

| Store | Minor or patch bump | Major bump |
| --- | --- | --- |
| LiteLLM, Langfuse web and worker, Caddy | `git checkout` the previous pin and `up -d`. Stateless. | Langfuse 3 to 4 is one-way after its cutover; restore the Checkpoint. |
| PostgreSQL | Same. Minors do not touch the data format. | A new major cannot open an older cluster. Upgrade with pgautoupgrade at the mount path, from a Checkpoint. Rollback is restore. |
| ClickHouse | Same, within a supported LTS line. | Move one LTS line at a time with Langfuse stopped. Rollback is restore. |
| RustFS | Same. | Read the notes; the on-disk format may change. Rollback is restore. |
| Valkey | Same. AOF is forward compatible across 9.x. | Drain the queue first: stop LiteLLM and Langfuse web, wait for the worker to idle, then upgrade. |

## Experience Changes

A bump that changes what an operator or a caller sees, even with compatible interfaces, is an Experience Change. The pull request names it and its rollback; that paragraph goes in the release notes.

## Retiring a pin

A version that fails the smoke contract does not merge. If a merged version fails on a host, revert the commit; the previous digest is still in the registry and in the local image cache until pruned. Do not `docker system prune` on a production host right after an upgrade.

## Required setup before production ingestion

For every Langfuse project, sign in as a project owner or administrator, open
**Project Settings → Data Retention**, set the retention in days (minimum 3), and save.
Start with 30 days unless the project's requirements specify otherwise. Confirm deletion
and disk growth after the nightly cleanup. Repeat for every new project. Automatic
retention on self-hosted Langfuse requires Enterprise Edition; if unavailable, production
setup requires an assigned owner and a scheduled, verified deletion procedure using
Langfuse's supported trace deletion UI/API. Leaving data indefinitely is not a retention
policy. Dataset items and audit logs need their own lifecycle.
See [Langfuse retention](https://langfuse.com/docs/administration/data-retention) and
[data deletion](https://langfuse.com/docs/administration/data-deletion).

Create bounded [consumer keys](keys.md) before distributing access. Configure monitoring
and a real alert receiver, then verify the queries in [backup](backup.md) and
[capacity](capacity.md). Resource limits are based on the measured 24 GB host; rehearse
on the production workload before raising traffic.

## External volumes and deliberate destruction

The six durable volumes are external, named `<LG_VOLUME_PREFIX>-<volume>`, with prefix
`llm-gateway-stack` by default. Bootstrap creates missing volumes. Changing
`COMPOSE_PROJECT_NAME` alone does not isolate storage; set a unique `LG_VOLUME_PREFIX`
for each installation. `docker compose down -v` no longer removes these data volumes
or the Postgres bind directory. Deliberate removal is `scripts/destroy.sh`: type the
exact project name when prompted. It refuses a non-empty or unreadable Postgres directory
unless `--include-postgres` is supplied. That option also deletes the cluster contents.
Backups and `.env` remain. The command accepts `--env-file`.

Existing installations with `<project>_<volume>` names need an offline copy before
starting this Compose revision. Take a Checkpoint using the old checkout first. Save
the old checkout and `.env` for rollback. With both checkouts available, run from the
new checkout after setting the actual old project and new prefix below:

```sh
old_project=llm-gateway-stack
volume_prefix=llm-gateway-stack
# Stop using the OLD Compose file, without -v, before copying any store.
docker compose -f /opt/gateway-old/compose.yaml --project-directory /opt/gateway-old down
pg_image=$(docker compose config --images postgres)
for volume in clickhouse-data clickhouse-logs rustfs-data valkey-data caddy-data caddy-config; do
  source_volume="${old_project}_${volume}"
  target_volume="${volume_prefix}-${volume}"
  docker volume inspect "$source_volume" >/dev/null || exit 1
  if docker volume inspect "$target_volume" >/dev/null 2>&1; then
    echo "Destination already exists: $target_volume" >&2; exit 1
  fi
  docker volume create "$target_volume" >/dev/null || exit 1
  docker run --rm --network none --user 0 --entrypoint sh \
    --mount "type=volume,src=$source_volume,dst=/source,readonly" \
    --mount "type=volume,src=$target_volume,dst=/target" "$pg_image" \
    -ec 'cp -a /source/. /target/; sync -f /target' || exit 1
done
```

Set `LG_VOLUME_PREFIX` in the new `.env` to the chosen prefix. Keep the old volumes
until restore and application checks pass. Do not start both incarnations together.
Before new writes, rollback is stopping the new project and starting the saved old
checkout with its original volumes. After new writes, restore a Checkpoint to avoid
silently losing those writes.

Monitoring also needs a new secret and role on existing clusters. Append only the new
secret to the protected original `.env`, then recreate Postgres to pass it through and
run the idempotent monitoring init script explicitly (normal init runs only on an empty
cluster). This does not recreate either application database:

```sh
python3 - <<'PYCODE'
from pathlib import Path
import re, secrets
path = Path('.env')
text = path.read_text()
if not re.search(r'^(?:export )?LG_POSTGRES_EXPORTER_PASSWORD=', text, re.M):
    with path.open('a') as handle:
        handle.write('\nLG_POSTGRES_EXPORTER_PASSWORD=' + secrets.token_hex(24) + '\n')
path.chmod(0o600)
PYCODE
docker compose up -d --no-deps --wait postgres
docker compose exec -T postgres bash /docker-entrypoint-initdb.d/02-monitor.sh
scripts/bootstrap.py
```

The `lg_monitor` login has `pg_monitor` and read-only transactions by default, with no
application write grants. Preserve `LG_POSTGRES_EXPORTER_PASSWORD` with the other secrets.

Before applying the Fable ingress changes to an existing installation, set a real
`LANGFUSE_INIT_USER_EMAIL`, mount `LG_BACKUP_DIR` on a filesystem separate from Postgres,
and append `UI_USERNAME=admin` and a generated `UI_PASSWORD` to the protected `.env`.
Bootstrap refuses to invent missing credentials when data already exists. Use the same
append-only secret-generation procedure above for `UI_PASSWORD` (24 random bytes).
Set `LG_TRUSTED_PROXIES` to the platform subnet behind the edge and choose specific
operator addresses for `LG_OPERATOR_ALLOW` and scraper addresses for `LG_CHECKPOINT_ALLOW`; see [ingress](ingress.md).

After completing the exporter role setup and credentials, run bootstrap and recreate the
stack in the maintenance window so healthchecks, PID/memory limits and exporters apply.
Verify the live limits without printing the resolved environment:

```sh
scripts/bootstrap.py
docker compose up -d --wait
docker compose ps -q | xargs docker inspect --format '{{.Name}} memory={{.HostConfig.Memory}} pids={{.HostConfig.PidsLimit}}'
```

Every long-lived container must have a nonzero memory limit. The PID limits are 4096 for
ClickHouse, 1024 for LiteLLM and both Langfuse services, and 512 for the other services.

Removing the forced one-minute `archive_timeout` command setting recreates PostgreSQL
on the next Compose apply, causing a database restart. To restore a timed archival
bound, set `ALTER SYSTEM SET archive_timeout='60s'; SELECT pg_reload_conf();` after
budgeting archive storage; `ALTER SYSTEM RESET archive_timeout; SELECT pg_reload_conf();`
returns to the upstream default.
