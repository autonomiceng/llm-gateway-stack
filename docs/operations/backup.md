# Backup and restore

Run from the checkout, with Python 3, Docker Compose and the pinned images available:

```sh
scripts/backup.sh
scripts/restore.sh /mnt/backups/20260917T020000000000Z
```

Both commands accept `--env-file /path/to/.env`. They use that installation's Compose
project, effective images and storage paths. `COMPOSE_PROJECT_NAME` selects another project.
Do not run Compose changes or other backup tools concurrently. The scripts lock the
env file and backup repository against bootstrap and another Checkpoint operation.
Backup attempts also lock checkout-wide console status, including attempts using
different env files.

## Storage and secrets

Set `LG_BACKUP_DIR` in `.env` to an existing, mounted repository, encrypted at rest
and replicated off-host over encrypted transport. Encryption and replication are the
operator's job; these scripts do neither. This setting is required. Bootstrap, backup
and restore default to refusing a backup directory on the Postgres data filesystem (compared using
`st_dev`; bootstrap reports `backup_dir_same_filesystem`).
Backup and restore check the configured path before Compose or diagnostics can write
there, then recheck the resolved mounts. A missing repository is never created by
diagnostics. A local directory on another filesystem works for a drill but
cannot survive loss of the host. Keep one archive repository per source cluster and
recovered incarnation. The filesystem must support ownership, atomic hard links and
fsync. NFS root squashing requires operator provisioning of compatible ownership.

For an explicitly opted-in development installation, create a dedicated directory such
as `~/backups/llm-gateway-dev` and set `LG_BACKUP_DIR` to its **absolute expanded path**
in `.env`, with `LG_ALLOW_SAME_FILESYSTEM_BACKUP=true`. The default is `false`; only
literal `true` and `false` are accepted. Exported settings override `.env`, including
an empty policy value, which is invalid. Bootstrap, backup and restore use the same
policy and warn on opt-in: disk loss affects both live data and backups. This development
choice does not meet the production durability contract in ADR-0002. Local Mode alone
does not enable it.

The opt-in waives only the filesystem separation check. Identical, nested and
symlink-resolved overlapping Postgres and backup paths remain forbidden, including
when the paths span mounts. Keep the repository dedicated and preserve its permissions,
archive publication checks, capacity monitoring and manifest verification. WAL archiving,
coordinated fencing and retention remain enabled. Schedule Checkpoints as below; merely
enabling WAL archiving does not create a whole-stack Checkpoint. For a development
restore, explicitly retain the policy in the target `.env` or shell along with its new
storage paths. Production still requires a separate filesystem and off-host copies.

The bind mounts refuse a missing directory. They cannot distinguish an unmounted disk
from a directory on an unexpected filesystem that still differs from the data filesystem:
check the expected mount with `findmnt`
before starting the stack and monitor it continuously. Postgres's entrypoint creates
`archive/`, owned by the pinned image's postgres user with mode 0700. The backup root
must be listable by container UIDs: ClickHouse enumerates its backups disk root at startup, so use 0755 on that root and restrict
access at the host/mount level. Never make it world-writable.

Each Checkpoint root is 0711, allowing traversal without directory listing. Its
`clickhouse/` directory is owned by UID 101 and the host operator's GID with mode 0750;
`backup.zip` has the same ownership and mode 0640. This lets ClickHouse and the operator
read the archive without granting access to other users. Restore applies the same
permissions. Other artifact directories and files are restricted to the operator.
Failed commands retain stdout and stderr in `LG_BACKUP_DIR/.diagnostics/` (0700), in
UTC timestamped logs (0600). Errors name a safe operation label and the log path.
If the diagnostics parent is missing or unwritable, the command still fails and reports
that diagnostics are unavailable. These logs can contain secrets; keep them private.
They are outside Checkpoint inventories.

Keep the **original `.env` separately**, in an encrypted password manager or protected
configuration backup, mode 0600 when on disk. Keep the matching Git checkout too.
The Checkpoint records only `.env` key names; it never copies the env file or serializes
its values. Database artifacts still contain sensitive application data and credentials.
They require the same protection as the live databases. A restore without the original
secrets is impossible: Langfuse `ENCRYPTION_KEY` and `SALT` come from
`LANGFUSE_ENCRYPTION_KEY` and `LANGFUSE_SALT`; LiteLLM needs `LITELLM_SALT_KEY`.
Bootstrap must not replace these with freshly generated values for recovery.
Checkpoint v1 records no secret fingerprints and cannot prove that a complete supplied
`.env` is the original. Completeness and shell-conflict checks do not authenticate it.
Health probes do not prove historical decryption. Recover `.env` from the original
protected backup and verify previously encrypted application values before reopening
traffic. Keep the target isolated from production callers until that check passes;
restore starts its gateway to run probes. Existing v1 Checkpoints retain this limitation;
their format is unchanged.

Compose shell precedence remains supported for deliberate recovery retargeting.
An exported `LG_POSTGRES_DATA_DIR`, `LG_VOLUME_PREFIX`, `LG_BACKUP_DIR`,
`COMPOSE_PROJECT_NAME` or `LG_ALLOW_SAME_FILESYSTEM_BACKUP` can override the saved file.
Restore warns with the affected setting names. Save the chosen target settings in its `.env`, or keep the identical
exported environment for every later Compose command; otherwise a later start may
select different storage. The source `.env` and source storage remain separate.

For an existing installation, create and mount `LG_BACKUP_DIR`, then apply the updated
Compose configuration during an operator-approved maintenance window. Postgres must
restart to enable `archive_mode=on`; ClickHouse must load `backups.xml`. Backups refuse
Postgres with archiving disabled. External volume names require the offline migration in [maintenance](maintenance.md).

## What a Checkpoint contains

Each UTC timestamp directory contains:

- `manifest.json`: completion timestamp, Git commit, every effective immutable image
  reference resolved through Compose and local Docker image metadata, env key names,
  fencing status, named Postgres restore point, timeline id, and byte size plus SHA-256
  for every artifact file.
- `postgres/`: `pg_basebackup` tar files, streaming WAL and PostgreSQL backup manifest.
- `wal/`: archived WAL through the named restore point at the Checkpoint's end.
- `clickhouse/backup.zip`: native `BACKUP DATABASE default`, Langfuse's database in
  this configuration. Langfuse owns the schema; the scripts add no DDL.
- `objects/`: an `aws s3 sync` copy of the entire RustFS `langfuse` bucket, including
  events, media and exports.
- `objects.meta.json`: media Content-Type and optional Content-Disposition/Content-Encoding,
  captured with `s3api head-object` in at most eight concurrent calls.
- `valkey.tar`: the complete multipart AOF directory, including its manifest.

The manifest is written last. A directory without it is incomplete and cannot be
restored. An interrupted backup retains its artifacts for inspection; remove incomplete
sets only after confirming no backup is running. Checksums detect corruption, not a
maliciously replaced manifest. Custom PostgreSQL tablespaces are refused.

Before creating capture artifacts or fencing, backup compares each project container's
image reference, image content ID, and persistent mounts with resolved Compose. It
refuses drift, including changed volume prefixes or Postgres paths. Take the Checkpoint
from the checkout and settings that started the running installation, before updating
its pins or storage settings. Stop and remove leftover one-off project containers first.

Every configured image, including helpers, must be available locally before backup.
Tags are resolved through Docker's local `RepoDigests` and checked against the local image
content ID. Local images without a registry digest are refused before capture or fencing;
publish and pull the image, then recreate the affected service before backing it up.
Checkpoint helpers never pull images during capture. Keep recorded registry digests
available for recovery and pull them before restore. Do not retag or prune images during
a Checkpoint operation. Overrides still need compatible commands, UIDs and data layouts.

The default fence stops Caddy, LiteLLM and Langfuse web in that order with a 120-second
shutdown grace per service. The worker stays running until three consecutive two-second
polls report no ready or active BullMQ work. Polling requires worker `/api/health` to
succeed and scans `bull:*` in Valkey: `LLEN` for `wait`, `active`, `paused`, `ZCARD` for
`prioritized` and ingestion `delayed` sets, and due jobs in other `delayed` sets. Future
recurring maintenance jobs and failed jobs remain in the AOF; idle does not mean every
job succeeded. Resolve failed ingestion jobs before relying on a Checkpoint.

After draining, the script stops the worker, checks queues again, then stops Valkey so
AOF writes and rotations cannot race the copy. Stopping the worker also prevents its
background database/object writes during capture. It attempts to resume all five services on
success, failure or catchable interruption. Resumption allows up to 120 seconds for
its initial Compose start (or unpause). After failed or interrupted capture, it waits
at least `--stop-timeout` plus 10 seconds (130 by default) before accepting healthy
services, allowing an in-flight daemon stop to settle. The Compose health deadline is
that settle interval plus 300 seconds; status and retry-start commands each have a
120-second limit, shortened to the remaining deadline. It then verifies gateway and
Langfuse health before retention or success publication. Reading the local certificate authority’s public root
allows another 120 seconds; Local Mode runs two HTTP and two HTTPS probes, each with a 120-second retry window
and five-second socket timeouts, so an in-flight request can overrun its retry window.
If capture and resumption both fail, the error reports both.

For a supervisor, budget the initial start, settle interval, health polling, optional
CA read and all four Local Mode gateway probes: 1150 seconds at the default shutdown grace before
HTTP request overruns and process cleanup. Allow **at least 1320 seconds** before a
forced kill, increasing both budgets by every second added to `--stop-timeout`. Under systemd,
set `TimeoutStopSec=1320` and `KillMode=mixed`: the initial stop signal must reach only
the backup parent so it can resume services. A separate session does not escape the
unit's cgroup. These are operational allowances, not a guaranteed RTO under stalled
host I/O. Capture commands have no new 120-second limit because legitimate base backups
and object copies can take much longer; a whole-job timeout must budget the complete
capture plus resumption.

On interruption or command timeout, the script kills its owned CLI process group,
including Compose plugin children, and reaps the direct child. Docker-daemon work already
submitted, such as a stop or an exec running `pg_basebackup`, can continue. Inspect and remove leftover
one-off containers only after confirming their work has ended; a later capture refuses
such containers.

Default drain timeout is 300 seconds;
`--fence-timeout SECONDS` changes it. A fenced Checkpoint requires the Langfuse
worker to log `Shutdown complete, exiting process` (its ClickHouse writer flush is done;
the node process then hangs and is killed at the timeout, which is an upstream quirk) and
Caddy, LiteLLM and Valkey to exit 0 so ingress drains and buffered spend, trace and queue writes finish.
Langfuse web waits 110 seconds before closing backend connections; capture requires
both its `Prisma connection has been closed.` and `Shutdown complete` messages from
this stop attempt. Its supervisor may then kill the remaining process. Missing web
or worker completion evidence, or an unclean Caddy, LiteLLM or Valkey stop, aborts
capture without a completed manifest. Rerun the backup; if unclean stops repeat, raise the shutdown grace with
`--stop-timeout SECONDS` (default and fenced minimum 120). A forced kill of the backup process or host failure
cannot execute cleanup: inspect the incomplete directory and start the fenced services manually.
Direct database/object writers must also be quiesced by the operator.

`scripts/backup.sh --no-fence` skips application stop/start and worker drain. It briefly
pauses Valkey while copying AOF to prevent rotation races. The result is **crash-consistent
only**, with no cross-store consistency guarantee; the manifest records `fenced: false`.
Media metadata is collected for the files actually synced. An object deleted before its
metadata lookup fails capture. Concurrent content/metadata edits can still represent
different moments; only fencing gives the documented consistency boundary.
Use the default fence for the Checkpoint before any persistent change. After a forced
kill during `--no-fence`, run `docker compose unpause valkey`; normal cleanup cannot
run after an uncatchable kill.

## Fresh restore

Fence the old installation and all its writers first. Keep its data and archive intact.
Use the exact immutable image references recorded in the manifest, setting the matching
`LG_*_IMAGE` variables in the target `.env` when needed. Tag overrides are captured as
registry digest references; restore refuses mutable tags even when they currently resolve
to the right content. Existing v1 Checkpoints with digest pins remain supported.
Restore refuses a running project,
any non-empty Postgres data directory, or **any non-empty project or configured external volume**, including
Caddy and log volumes. It does not delete existing data to make a restore fit.
A manifest with `fenced: false` is refused unless `--allow-unfenced` explicitly accepts
the absence of cross-store consistency. Checkpoints without a timeline id require their
matching old checkout to restore.

1. Obtain the matching checkout and original `.env` through the separate secure channel.
   Every managed secret must be present and non-empty in that file. Backup and restore
   reject duplicate managed settings and conflicting exported secrets under the env lock,
   before resolving Compose or writing target data. Unset conflicting shell values.
   Set fresh `LG_POSTGRES_DATA_DIR` and `LG_BACKUP_DIR` paths and a unique `LG_VOLUME_PREFIX`. The latter needs an empty
   `archive/` or no archive directory, so the recovered timeline cannot overwrite the
   source's WAL. Create the backup root and verify its mount and permissions.
2. Supply the original `.env` and an existing, mounted `LG_BACKUP_DIR`; no prior
   `--render-only` is required. Restore creates a missing Postgres data directory with
   mode 0755 and prepares `data/console`, then checks target storage for emptiness and creates missing external volumes.
   Restore also ensures the configured shared network exists before writing target data.
   Normal bootstrap starts services and makes their data non-empty, so skip it.
3. Run `scripts/restore.sh /path/to/original/Checkpoint`. It validates image pins and all
   artifact hashes before writing target data. It copies the Checkpoint into the new
   backup root through an incomplete `.restoring` directory if necessary and re-verifies
   the copy before publishing its final name and extracting it, then unpacks and verifies the Postgres base backup
   and recovers
   to the named end point on the recorded `recovery_target_timeline`, with
   `recovery_target_action=promote`. After promotion it removes `checkpoint-wal` and
   resets `restore_command` and all `recovery_target_*` settings. It restores the AOF,
   starts storage only, uses ClickHouse `RESTORE`, creates the bucket using the pinned
   rustfs-init image and restores events/exports with `application/json`, then media with
   the content headers in `objects.meta.json`.
4. The script starts the full stack, waits for Compose health and probes
   `/health/litellm` and `/health/langfuse`. Verify application data and login before
   routing production traffic. Public Mode requires working DNS, certificates and trust
   on the recovery host; Caddy's certificate volumes are regenerated, not restored.

A failed restore leaves partial data for diagnosis and keeps applications stopped until
all stores have been restored. If failure happens during the final startup/probes,
applications may already be running. Fix the cause and use another empty target; do not
rerun over partial data. Retain the source Checkpoint until recovery is verified.

## RPO, RTO and the drill

**RPO is the Checkpoint interval; the WAL archive supports manual point-in-time recovery by an expert.**
With daily successful off-host Checkpoints, the whole-stack target is at most 24 hours.
A failed/missing backup or missing off-host copy invalidates that bound.
Postgres keeps its default `archive_timeout=0`: completed WAL segments archive normally,
and Checkpoint capture explicitly switches WAL before waiting for its restore point.
This avoids a mostly empty 16 MiB segment every minute under light write activity,
which can otherwise approach 22.5 GiB/day. It provides no timed database-only archival
bound between Checkpoints. Operators needing one must budget archive capacity and
set `ALTER SYSTEM SET archive_timeout='5min'; SELECT pg_reload_conf();` after budgeting storage. Manual Postgres
PITR requires a usable base backup, every subsequent WAL segment and reconciliation
with ClickHouse, objects and Valkey. The restore command stops at the Checkpoint's named
end point; it has no `--target-time` option.

**RTO has no guarantee.** The operational drill target is 60 minutes for its small mock
dataset. The drill prints measured RTO in seconds from disposable teardown through
fresh preparation, restore and application verification. It excludes backup creation,
fetching off-host artifacts, installing Docker and pulling images. Production RTO must
be measured with representative data, storage throughput and external DNS/TLS steps.
No measured RTO is claimed until the drill exits zero on the target host.

Run monthly and after backup or pin changes from a clean disposable checkout, without
`.env`, `data/`, or local Compose override files. Bootstrap versions and Checkpoint
metrics use checkout-wide `data/console`; the drill refuses a checkout with installation
state. Use a fresh checkout for each run.

```sh
SMOKE_PROJECT=llm-gateway-drill SMOKE_HTTP_PORT=18090 scripts/backup-drill.sh
```

Default HTTPS port is 18453; `SMOKE_HTTPS_PORT` can change it. The drill accepts only
`llm-gateway-drill` or a name beginning `llm-gateway-drill-`, refuses an existing project,
uses Postgres storage under the checkout's `.scratch/` and backup repositories under
`SMOKE_BACKUP_ROOT` (default `/tmp`). That root must exist on a different filesystem
from `.scratch/`; the drill checks this before creating files or Docker resources.
CI mounts a disposable tmpfs there. The drill removes only its disposable volumes
and temporary files; generated console state remains in the disposable checkout.
It creates and removes its own `<project>-platform` network and probes its own HTTP port.
It never uses the installed or smoke project. It creates LiteLLM virtual key A, sends
two mock completions with it, waits for both observation IDs and event objects, takes a
fenced Checkpoint, creates key B after the Checkpoint, deletes its stores,
prepares a fresh install using the same secrets, and restores. Before sending a new
completion it checks both original `litellm_request` observation IDs through
`GET /api/public/v2/observations`, event object keys and ETags, a media object's `image/png` type and inline disposition, the Langfuse health POST,
a credentials login, authenticated session and project page. It proves key A still works
and key B is rejected, checking that recovery excludes writes after the Checkpoint.
It repeats capture, wipe, restore and all these checks for a second cycle, taking the
second Checkpoint from the recovered installation. Both cycles must pass. `/tmp` must
be on a different filesystem from the checkout and have room for the backup artifacts.
The media sample is put through `s3api` with explicit metadata; the browser/presigned
upload flow is not exercised. A Valkey sentinel is not required by this drill; it does not independently prove queue
content recovery. Any failed assertion exits nonzero. Failed commands write private
timestamped diagnostics in the drill's work directory; cleanup retains diagnostic logs
and their supporting files and prints their location.

## Known limitations

Media Content-Type, Content-Disposition and Content-Encoding survive restore. Events and
exports use `application/json`, including NDJSON as requested by this restore contract.
Custom object metadata, tags, ACLs, version history, cache-control and other S3 headers
are not captured. The filesystem copy assumes Langfuse-generated keys that map to regular
files; arbitrary S3 keys containing path traversal, directory markers or file/directory
collisions are unsupported. The drill does not prove queue contents or presigned media uploads.
Old Checkpoints without the sidecar require their matching old checkout to restore.

## Retention and deliberate deletion

`LG_BACKUP_KEEP` defaults to 7 complete Checkpoints and must be a positive integer.
After capture and successful service resumption, the script deletes older completed
Checkpoint directories beyond that count. Incomplete directories are retained for diagnosis.
With fewer than two complete Checkpoints, it deletes neither Checkpoints nor archive files.

Archive cleanup uses `postgres/backup_manifest` WAL-Ranges Start-LSN values and the
server's WAL segment size. It retains every segment from the earliest base-backup start
among the retained Checkpoints onward. Like `pg_archivecleanup`, comparison ignores the
timeline prefix; the boundary segment and all timeline history files stay. Older segments,
partial segments and backup history files are removed. Missing/invalid base manifests
abort cleanup before deletion. Keep the repository dedicated to this source cluster.

Use an off-host mounted repository or finish replication before the next retention cycle;
cleanup does not wait for a replication service or prove that off-host copies exist.
Independent copies need their own coordinated retention policy. Allow room for the new
Checkpoint before old ones are deleted, plus continuous WAL between runs. Failed backups
never trigger pruning, so alert on age and free space. Never manually remove live `pg_wal`.

`docker compose down -v` no longer removes durable volumes or Postgres data. The guarded,
deliberate removal command is `scripts/destroy.sh`, with typed project confirmation and
`--include-postgres` required for a non-empty Postgres directory; see [maintenance](maintenance.md).

## Scheduling and monitoring

Example daily schedule, with the checkout's protected `.env` and a mounted backup root:

```cron
0 2 * * * cd /opt/llm-gateway-stack && scripts/backup.sh >> /var/log/llm-gateway-backup.log 2>&1
```

The observability stack should alert on **archive failure** (`pg_stat_archiver.failed_count`
increasing, failed or stale archive progress during writes, off-host replication lag),
**Checkpoint age** (newest complete manifest older than the interval), and **mount missing**
(the expected backup mount is absent or unwritable). Also watch disk capacity and failed
drills. The scripts prune completed Checkpoints and archive segments together, but do
not schedule runs or send alerts.

The archive publication pattern follows the backplane: copy to a temporary file, fsync,
atomically link under the segment name, and reject an existing name with different bytes.
See [PostgreSQL recovery targets](https://www.postgresql.org/docs/18/runtime-config-wal.html#RUNTIME-CONFIG-WAL-RECOVERY-TARGET)
and [ClickHouse native backups](https://clickhouse.com/docs/concepts/features/backup-restore/overview)
for the upstream recovery mechanisms.

The gateway writes `data/console/metrics.txt` atomically: `lg_checkpoint_timestamp_seconds`
is the last successful capture, retention and service resumption; `lg_checkpoint_success`
is 0 during capture or after failure (including preflight refusal after acquiring the
invocation locks) and 1 after success. Refused lock-contending invocations leave the
active attempt's metrics unchanged; failures preserve the last-success timestamp.
Until the first run, the file is
absent and the scrape fails. Caddy serves `/metrics` at `lg-gateway:8081`, restricted
to socket peers in `LG_CHECKPOINT_ALLOW`; include the scraper address or its dedicated
network CIDR. This does not grant access to operator routes. Port 8081 is never
published, and port 80 returns 404 for checkpoint metrics. A stopped gateway during fencing causes a temporary scrape failure.
The PostgreSQL exporter supplies `pg_up`, `pg_stat_archiver_failed_count` and
`pg_stat_archiver_last_archive_age`. Verify in the observability query UI:

```promql
pg_up{job="llm-gateway-postgres"} == 1
increase(pg_stat_archiver_failed_count{job="llm-gateway-postgres"}[15m])
pg_stat_archiver_last_archive_age{job="llm-gateway-postgres"}
time() - lg_checkpoint_timestamp_seconds{job="llm-gateway-checkpoints"}
lg_checkpoint_success{job="llm-gateway-checkpoints"}
```

An idle server can have old archive progress; investigate stale progress during writes.
`pg_isready` health does not prove archiving or disk writability. Verify the scrape jobs,
alerts and a real notification receiver in the observability stack before production.

### WAL growth and archive failures

Use durable storage for `LG_BACKUP_DIR`; a RAM-backed `/tmp` or tmpfs mount is only
suitable for a disposable drill. Filesystem separation alone does not prove durability.
Monitor `pg_stat_archiver`, archive filesystem free space and live `pg_wal` size.
Failed archiving retains WAL regardless of `max_wal_size`; lowering that setting
cannot release unarchived segments. Check `pg_replication_slots` separately.

Repair archive availability and permissions while preserving existing archives and
the cluster. After required segments archive successfully, PostgreSQL checkpoints
can recycle eligible WAL. Never delete files from live `pg_wal`, use `pg_resetwal`
to reclaim space, or make the archive command report success without saving data.
When moving an archive, preserve its timeline history and all retained Checkpoint
requirements; ensure the destination can hold the pending WAL before resuming.

See [PostgreSQL archiving settings](https://www.postgresql.org/docs/18/runtime-config-wal.html#RUNTIME-CONFIG-WAL-ARCHIVING).

The web drain check follows the [pinned Langfuse shutdown implementation](https://github.com/langfuse/langfuse/blob/v4.37.0/web/src/utils/shutdown.ts).
It covers the upstream drain boundary; clients must retry requests without a successful
response, and direct writers must remain quiesced throughout capture.

Development same-filesystem backups share capacity with the live database: archive or
Checkpoint growth can fill the live data filesystem. Monitor free space and backup failures.
`scripts/backup-drill.sh` continues to require a separate filesystem; exercise the development
opt-in using an explicitly configured disposable restore target. Existing installations must
move nested backup mounts outside the Postgres data path before upgrading: overlapping
resolved paths are now refused even when their filesystem devices differ.

Resumption starts only fenced services with `up --no-deps --no-recreate`, preserving
existing containers and avoiding dependencies on removed one-shot initialization containers.
Datastores and initialization state must already satisfy capture preflight. A completed
manifest is printed before resumption; wait for the command exit and success metric before
claiming the whole backup operation succeeded.
