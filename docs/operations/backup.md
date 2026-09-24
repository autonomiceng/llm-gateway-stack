# Backup and restore

Take, keep and restore Checkpoints, and prove the pair with the drill.

- [Storage](#storage)
- [Secrets](#secrets)
- [What a Checkpoint contains](#what-a-checkpoint-contains)
- [Fencing](#fencing)
- [Restore](#restore)
- [RPO, RTO and the drill](#rpo-rto-and-the-drill)
- [Retention](#retention)
- [Scheduling and monitoring](#scheduling-and-monitoring)
- [Known limitations](#known-limitations)

Run from the checkout, with Python 3, Docker Compose and the pinned images available:

```sh
scripts/backup.sh
scripts/restore.sh /mnt/backups/20260917T020000000000Z
```

Both accept `--env-file /path/to/.env` and use that installation's Compose project, images
and storage paths. They lock the env file and the backup repository against bootstrap and
another Checkpoint operation; do not run other Compose changes or backup tools meanwhile.

## Storage

`LG_BACKUP_DIR` is the backup repository; the default is `./backups` in the checkout.

- **Local Mode** creates a missing repository with mode 0755. Backups may share the
  Postgres data filesystem; bootstrap, backup and restore then warn that disk loss affects
  both and that backup growth can fill the live data disk.
- **Public and Proxy Mode** require an existing, mounted repository on a filesystem separate
  from Postgres data (compared by `st_dev`; bootstrap reports `backup_dir_same_filesystem`).
  `LG_ALLOW_SAME_FILESYSTEM_BACKUP=true` accepts the shared filesystem with the same warning.
  Only literal `true` and `false` are valid, and an exported value overrides `.env`.

Identical, nested or symlink-resolved overlapping backup and Postgres paths are always
refused. Encryption at rest and off-host replication are the operator's job; a repository
on the same host cannot survive losing it. Check the expected mount with `findmnt` before
starting the stack: the bind mounts refuse a missing directory but cannot tell an unmounted
disk from an unexpected directory. The filesystem must support ownership, atomic hard links
and fsync; NFS root squashing needs compatible ownership provisioned by the operator.

Permissions: the repository root is 0755 because ClickHouse lists it at startup; restrict
access at the host or mount level and never make it world-writable. Postgres creates
`archive/` (0700, its own user). Each Checkpoint root is 0711; its `clickhouse/` directory
is owned by UID 101 and the operator's group (0750, `backup.zip` 0640); everything else
belongs to the operator. Failed commands keep stdout and stderr in
`LG_BACKUP_DIR/.diagnostics/` (0700, logs 0600); errors name only an operation label and the
log path. These logs can contain secrets.

## Secrets

Keep the original `.env` separately, in a password manager or protected configuration
backup (0600 on disk), with the matching Git checkout. A Checkpoint records only `.env` key
names. Restore is impossible without the original secrets: Langfuse needs
`LANGFUSE_ENCRYPTION_KEY` and `LANGFUSE_SALT`, LiteLLM needs `LITELLM_SALT_KEY`. Checkpoint v1
cannot prove a supplied `.env` is the original, and health probes do not prove historical
decryption: verify previously encrypted values before routing traffic to a restored stack.

## What a Checkpoint contains

Each UTC timestamp directory holds:

- `manifest.json`: completion time, Git commit, every image as an immutable reference, env
  key names, `fenced`, the Postgres restore point and timeline, and size plus SHA-256 of
  every artifact. It is written last; a directory without it is incomplete.
- `postgres/`: `pg_basebackup` tar files and backup manifest; `wal/`: archived WAL through
  the restore point.
- `clickhouse/backup.zip`: native `BACKUP DATABASE default`.
- `objects/` and `objects.meta.json`: the RustFS `langfuse` bucket, and media Content-Type,
  Content-Disposition and Content-Encoding.
- `valkey.tar`: the AOF directory.

Before any fencing, backup compares every project container's image, image content and
persistent mounts with resolved Compose and refuses drift or leftover one-off containers.
Every image must be local and resolve to a registry digest; keeping those images
available (a registry or a tested image archive) is separate from the data Checkpoint. Custom Postgres
tablespaces are refused.

## Fencing

Backup stops Caddy, LiteLLM and Langfuse web (120-second grace each), waits for three
consecutive idle polls of the BullMQ queues (`--fence-timeout`, default 300 seconds), stops
the worker, rechecks active jobs, then stops Valkey. Capture requires clean exits and each
application's shutdown-complete log line from this stop; otherwise it aborts without a
manifest. Raise the grace with `--stop-timeout` (minimum 120) if clean stops fail repeatedly.

On success, failure or a catchable signal, backup restarts the stopped services with
`up --no-deps --no-recreate`, waits for their health and probes the gateway before pruning
or reporting success. A forced kill or host failure skips that: start the services manually.
Budget at least 1320 seconds before a supervisor kills a backup; under systemd use
`TimeoutStopSec=1320` and `KillMode=mixed` so the stop signal reaches only the parent.

Direct writers outside the stack must be quiesced by the operator. Failed ingestion jobs
remain in the AOF; resolve them before relying on a Checkpoint.

## Restore

Restore needs an empty target: fence the old installation and keep its data and archive.

1. Obtain the matching checkout and the original `.env`. Every managed secret must be
   present and not overridden by a different exported value. Set fresh
   `LG_POSTGRES_DATA_DIR` and `LG_BACKUP_DIR` paths and a unique `LG_VOLUME_PREFIX`, and
   create the new repository (0755). It must have an empty or no `archive/`, so the
   recovered timeline never writes into the source's WAL. Pull the exact image references in the manifest, setting
   `LG_*_IMAGE` when they differ from the pins. Checkpoints that include the datastore
   exporters need `LG_METRICS=true` and `COMPOSE_PROFILES=metrics` in the target `.env`.
2. Run `scripts/restore.sh /path/to/Checkpoint` without bootstrapping first. It verifies
   images and artifact hashes, creates the Platform Network with the `.env` allocation,
   refuses any non-empty Postgres directory or project volume, copies the Checkpoint into the
   new repository and re-verifies it, recovers Postgres to the named restore point and
   promotes, restores Valkey, ClickHouse and objects with their media headers, starts the
   stack, probes the gateway and then writes the Status Document.
3. Verify application data and login before routing production traffic. Public Mode needs
   DNS and certificates on the recovery host; Caddy volumes are regenerated, not restored.

A manifest with `fenced: false`, from releases that could take unfenced backups, is refused
unless `--allow-unfenced` accepts the missing cross-store consistency. Exported
`LG_POSTGRES_DATA_DIR`, `LG_VOLUME_PREFIX`, `LG_BACKUP_DIR`, `COMPOSE_PROJECT_NAME` or
`LG_ALLOW_SAME_FILESYSTEM_BACKUP` override `.env`; restore names them, so save them in the
target `.env` or keep them for every later command. A failed restore leaves partial data
for diagnosis: fix the cause and restore into another empty target.

## RPO, RTO and the drill

**RPO is the Checkpoint interval.** Daily successful off-host Checkpoints bound whole-stack
loss at 24 hours; a failed backup or a missing off-host copy breaks that bound. The WAL
archive supports manual point-in-time recovery by an expert, reconciled with ClickHouse,
objects and Valkey; restore itself stops at the Checkpoint's restore point. Postgres keeps
`archive_timeout=0`; for a timed archive bound, budget the storage and run
`ALTER SYSTEM SET archive_timeout='5min'; SELECT pg_reload_conf();`.

**RTO has no guarantee.** The drill target is 60 minutes for its small dataset; measure
production RTO with representative data. Run the drill monthly and after backup or pin
changes, from a fresh disposable checkout without `.env`, `data/` or Compose overrides:

```sh
SMOKE_PROJECT=llm-gateway-drill SMOKE_HTTP_PORT=18090 scripts/backup-drill.sh
```

The HTTPS port defaults to 18453 (`SMOKE_HTTPS_PORT`); `SMOKE_PLATFORM_SUBNET` replaces the
derived `172.31.x.0/24` network. Backup repositories go under `SMOKE_BACKUP_ROOT` (default
`/tmp`), which must be a different filesystem from the checkout's `.scratch/`. The drill
refuses an existing project and removes only its own volumes, network and files. Twice, it
takes a fenced Checkpoint, wipes the stores and restores into fresh storage, then checks the
original observations, event object ETags, media headers, a Langfuse login, that a key made
before the Checkpoint works and one made after it is rejected, and prints the measured RTO.
It does not prove queue contents or browser media uploads.

## Retention

`LG_BACKUP_KEEP` (default 7) complete Checkpoints are kept. After a successful capture and
resumption, backup deletes older complete Checkpoints and the WAL segments before the oldest
retained base backup; timeline history files and incomplete directories stay. With fewer
than two complete Checkpoints nothing is deleted. Finish off-host replication before the next
run; retention does not wait for it. Never delete files from the live `pg_wal`.

`docker compose down -v` does not remove the external volumes or Postgres data; deliberate
removal is `scripts/destroy.sh` (see [maintenance](maintenance.md)).

## Scheduling and monitoring

```cron
0 2 * * * cd /opt/llm-gateway-stack && scripts/backup.sh >> /var/log/llm-gateway-backup.log 2>&1
```

Backup writes `data/console/metrics.txt`: `lg_checkpoint_timestamp_seconds` (last success)
and `lg_checkpoint_success` (0 during capture and after failure). Caddy serves it at
`lg-gateway:8081/metrics` to peers in `LG_CHECKPOINT_ALLOW`; port 8081 is never published.
With `LG_METRICS=true` the PostgreSQL exporter adds the archiver metrics. Alert on archive
failures, Checkpoint age, a missing backup mount, and disk capacity:

```promql
pg_up{job="llm-gateway-postgres"} == 1
increase(pg_stat_archiver_failed_count{job="llm-gateway-postgres"}[15m])
pg_stat_archiver_last_archive_age{job="llm-gateway-postgres"}
time() - lg_checkpoint_timestamp_seconds{job="llm-gateway-checkpoints"}
lg_checkpoint_success{job="llm-gateway-checkpoints"}
```

Failed archiving retains WAL regardless of `max_wal_size`. Repair archive storage and
permissions while preserving existing archives; never use `pg_resetwal` or make the archive
command report success without saving data.

## Known limitations

Events and exports are restored as `application/json`; custom object metadata, tags, ACLs,
versions and cache headers are not captured. Object keys must map to regular files. Old
Checkpoints without the metadata sidecar or a timeline id need their matching old checkout.
See [PostgreSQL recovery targets](https://www.postgresql.org/docs/18/runtime-config-wal.html#RUNTIME-CONFIG-WAL-RECOVERY-TARGET)
and [ClickHouse backups](https://clickhouse.com/docs/concepts/features/backup-restore/overview).
