# Maintenance

How to apply a version bump, override an image, read the status document, roll back, and
destroy an installation on purpose. Default versions are pinned by digest in
`compose.yaml`. Renovate opens a pull request when an upstream releases; nothing changes
on a host until an operator merges and pulls.

- [Before touching a host](#before-touching-a-host)
- [Applying a bump](#applying-a-bump)
- [Rollback boundary per store](#rollback-boundary-per-store)
- [Image overrides](#image-overrides)
- [Status document](#status-document)
- [Required setup before production ingestion](#required-setup-before-production-ingestion)
- [Resource limits](#resource-limits)
- [External volumes and deliberate destruction](#external-volumes-and-deliberate-destruction)
- [Older installations](#older-installations)

## Before touching a host

1. Read the release notes for every image that changed. Langfuse, Postgres and ClickHouse
   majors are one-way for data.
2. Run `scripts/smoke.sh` on the commit you are about to deploy. CI runs it on every PR,
   weekly, and on demand (workflow dispatch); run it where you can see the output.
3. Take a Checkpoint: `scripts/backup.sh`. This is the rollback boundary.

## Applying a bump

```sh
git pull
docker compose pull
python3 scripts/bootstrap.py
```

Bootstrap records any setting a release adds (the literal `COMPOSE_FILE`, `COMPOSE_PROFILES`
for `LG_METRICS`), recreates what changed, waits for the gateway health probes and refreshes
the status document. For the exporter profile, read [metrics](ingress.md#metrics) before the
next Checkpoint.

Compose recreates only the containers whose image or configuration changed. Datastores
keep their volumes. Expect a few minutes of gateway downtime while LiteLLM and Langfuse
restart; callers see connection errors, not wrong answers.

A bump that changes what an operator or a caller sees, even with compatible interfaces, is
an Experience Change. The pull request names it and its rollback; that paragraph goes in
the release notes.

## Rollback boundary per store

| Store | Minor or patch bump | Major bump |
| --- | --- | --- |
| LiteLLM, Langfuse web and worker, Caddy | `git checkout` the previous pin and `up -d`. Stateless. | Langfuse 3 to 4 is one-way after its cutover; restore the Checkpoint. |
| PostgreSQL | Same. Minors do not touch the data format. | A new major cannot open an older cluster. Upgrade with pgautoupgrade at the mount path, from a Checkpoint. Rollback is restore. |
| ClickHouse | Same, within a supported LTS line. | Move one LTS line at a time with Langfuse stopped. Rollback is restore. |
| RustFS | Same. | Read the notes; the on-disk format may change. Rollback is restore. |
| Valkey | Same. AOF is forward compatible across 9.x. | Drain the queue first: stop LiteLLM and Langfuse web, wait for the worker to idle, then upgrade. |

A version that fails the smoke contract does not merge. If a merged version fails on a
host, revert the commit; the previous digest is still in the registry and in the local
image cache until pruned. Do not `docker system prune` on a production host right after an
upgrade.

## Image overrides

Copy the relevant empty `LG_*_IMAGE` assignment from `.env.example` into `.env`, and
supply a complete reference such as `registry.example/gateway:trial` or
`registry.example/gateway@sha256:<digest>`. Every service has an override, including the
AWS CLI helper. Langfuse web and worker use separate references and must stay on matching
versions. Empty or unset values use the inline default. Exported values take precedence,
including an empty value selecting the default. Normal `docker compose` commands apply.

Take a Checkpoint before changing a running installation's images. Preserve the previous
settings for rollback; the data migration boundaries above also apply to overrides. Restore
requires the exact immutable references in the Checkpoint; see [backup](backup.md). Local
images can run, but need a registry digest before Checkpoint capture. Refresh the status
document with bootstrap after applying a change. `validate.sh` checks isolated repository
defaults; it does not certify operator overrides. Validate experiments in a disposable
installation before applying them to persistent data.

Renovate's Docker Compose manager supports inline defaults, covered by its
[default-variable extraction test](https://github.com/renovatebot/renovate/blob/main/lib/modules/manager/docker-compose/extract.spec.ts).
Langfuse grouping and major-update maintenance labels remain unchanged.

## Status document

Bootstrap writes `data/console/status.json` after the gateway passes its readiness
probes, replacing the whole file at once. Caddy serves it as `GET /status.json` to any
client, in every access mode, with `Cache-Control: no-store`. It follows Status v2 in
[conventions](../conventions.md): the configured image of each component without its
digest, the tag as `version` (null for a tag that is not a release), whether the selected
Compose profiles enable the service, the LiteLLM, Langfuse and S3 origins, and the time of
the newest Checkpoint in `LG_BACKUP_DIR` when bootstrap ran; later Checkpoints do not
update it. It is configuration, not observation: a version is what bootstrap configured,
not what runs. Rerun bootstrap after changing images, origins or profiles. The document
never contains secrets, container names or host paths.

Liveness comes from `/health/<component>`, a status with an empty body for every client:

| Component | Probe |
| --- | --- |
| `caddy` | Caddy answers |
| `litellm`, `langfuse-web`, `langfuse-worker` | `/health/readiness`, `/api/public/health`, worker `/api/health` |
| `clickhouse`, `rustfs` | `/ping`, `/health/live` |
| `postgres` | Langfuse `/api/public/health?failIfDatabaseUnavailable=true` |
| `postgres-exporter`, `valkey-exporter` | landing page, `/health` |
| `valkey` | none over HTTP; always 404, read as unknown |

## Required setup before production ingestion

For every Langfuse project, sign in as a project owner or administrator, open
**Project Settings > Data Retention**, set the retention in days (minimum 3), and save.
Start with 30 days unless the project's requirements specify otherwise. Confirm deletion
and disk growth after the nightly cleanup. Repeat for every new project. Automatic
retention on self-hosted Langfuse requires Enterprise Edition; if unavailable, production
setup requires an assigned owner and a scheduled, verified deletion procedure using
Langfuse's supported trace deletion UI or API. Leaving data indefinitely is not a retention
policy. Dataset items and audit logs need their own lifecycle.
See [Langfuse retention](https://langfuse.com/docs/administration/data-retention) and
[data deletion](https://langfuse.com/docs/administration/data-deletion).

Create bounded [consumer keys](keys.md) before distributing access. Configure monitoring
and a real alert receiver, then verify the queries in [backup](backup.md) and
[capacity](capacity.md). Resource limits are based on the measured 24 GB host; rehearse
on the production workload before raising traffic.

## Resource limits

Every long-lived container has a nonzero memory limit and a PID limit: 4096 for
ClickHouse, 1024 for LiteLLM and both Langfuse services, and 512 for the other services.
Verify the live limits without printing the resolved environment:

```sh
docker compose ps -q | xargs docker inspect --format '{{.Name}} memory={{.HostConfig.Memory}} pids={{.HostConfig.PidsLimit}}'
```

The values and the reasoning are in [capacity](capacity.md).

## External volumes and deliberate destruction

The six durable volumes are external, named `<LG_VOLUME_PREFIX>-<volume>`, with prefix
`llm-gateway-stack` by default. Bootstrap creates missing volumes. Changing
`COMPOSE_PROJECT_NAME` alone does not isolate storage; set a unique `LG_VOLUME_PREFIX`
for each installation. `docker compose down -v` does not remove these data volumes or the
Postgres bind directory. Deliberate removal is `scripts/destroy.sh`: type the exact
project name when prompted. It refuses a non-empty or unreadable Postgres directory unless
`--include-postgres` is supplied; that option also deletes the cluster contents. Backups
and `.env` remain. The command accepts `--env-file`.

## Older installations

Installations created before the current bootstrap need a few one-time steps after
`git pull`. Each is safe to repeat.

- **Status timer.** Run `scripts/retire-status-timer.sh` as the installation user, then
  `python3 scripts/bootstrap.py`. The script disables and removes
  `llm-gateway-status.timer` and `.service` from the user's systemd directory, reloads the
  user manager, and deletes `data/status/bootstrap.json` and `data/console/.status.lock`.
  Bootstrap then writes the Status v2 `status.json`; `/versions.json` is gone.
- **Literal `COMPOSE_FILE`.** Run `python3 scripts/bootstrap.py --render-only` before
  `docker compose pull` or any other direct Compose command. It replaces a recorded
  `compose.${LG_ACCESS_MODE:-local}.yaml` token with the literal list
  (`compose.local.yaml` no longer exists) and starts nothing. The following full bootstrap
  recreates Valkey, which now reads its password from a Compose config file instead of its
  command line.
- **Removed settings.** Delete from `.env` the Langfuse v3-to-v4 migration write-mode
  line, the operator allow list, the RustFS console switch and the Grafana and Backplane
  link URLs; the gateway no longer reads them. Applications authenticate themselves, the
  RustFS console is always on, and Edge owns the links between stacks.
- **Required values.** Set a real `LANGFUSE_INIT_USER_EMAIL`; set `LG_BACKUP_DIR` (in
  Public and Proxy Mode, a mount separate from Postgres); append `UI_USERNAME=admin` and a
  generated `UI_PASSWORD` to the protected `.env`, since bootstrap refuses to invent
  missing credentials when data already exists. Behind Edge, set
  `LG_TRUSTED_PROXIES=172.30.0.2/32` or delete the line to use that default; bootstrap
  keeps a nonempty older value and refuses one that overlaps the dynamic range. Choose
  scraper addresses for `LG_CHECKPOINT_ALLOW` ([ingress](ingress.md#metrics)); scrapers
  now read LiteLLM metrics at `lg-gateway:8081/metrics/litellm`.
- **Backups.** Every new Checkpoint is fenced; restore still accepts an older unfenced one
  with `--allow-unfenced`.
- **Monitoring role on an existing cluster.** Init scripts run only on an empty cluster, so
  add the exporter secret and role by hand. Append only the new secret to the protected
  `.env`, recreate Postgres to pass it through, and run the idempotent monitoring script:

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
  python3 scripts/bootstrap.py
  ```

  Use the same append-only procedure for `UI_PASSWORD` (24 random bytes). The
  `lg_monitor` login has `pg_monitor` and read-only transactions, with no application
  write grants. Preserve `LG_POSTGRES_EXPORTER_PASSWORD` with the other secrets.
- **Volume names.** Installations with `<project>_<volume>` volume names need an offline
  copy before starting this Compose revision. Take a Checkpoint with the old checkout
  first and keep it, with its `.env`, for rollback. Stop the old project without `-v`,
  then run from the new checkout after setting the old project name and the new prefix:

  ```sh
  old_project=llm-gateway-stack
  volume_prefix=llm-gateway-stack
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
  checkout with its original volumes. After new writes, restore a Checkpoint.
- **Postgres archive timeout.** The forced one-minute `archive_timeout` command setting is
  gone; the next Compose apply recreates PostgreSQL once. To restore a timed archival
  bound, budget archive storage and run
  `ALTER SYSTEM SET archive_timeout='60s'; SELECT pg_reload_conf();`;
  `ALTER SYSTEM RESET archive_timeout; SELECT pg_reload_conf();` returns to the upstream
  default.

After these steps, run bootstrap in the maintenance window so healthchecks, PID and memory
limits and exporters apply, and verify the limits as shown in [resource limits](#resource-limits).
