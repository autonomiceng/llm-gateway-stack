# Host capacity

The Reference Stack has ten long-lived containers plus the bucket initializer. Default
limits target the measured 24 GB host: use 8 CPU cores, 24 GB RAM, SSD storage with at
least 40 GB for images and initial data, and an encrypted off-host backup mount. For
sustained ingestion, start with 32 GB RAM and 200 GB NVMe and measure the real workload.
A shared host must also budget separately for the other stacks.

## Enforced memory envelope

Idle figures were supplied from `docker stats` on the 24 GB host. Reservations are soft
limits under memory contention; limits are hard cgroup ceilings. They do not preallocate
RAM or prevent host OOM if other workloads consume the remaining memory.

| Service | Measured idle MiB | Reservation | Limit |
| --- | ---: | ---: | ---: |
| Caddy | 12 | 32 MiB | 128 MiB |
| ClickHouse | 350 | 1 GiB | 8 GiB |
| Langfuse web | 830 | 1 GiB | 2 GiB |
| Langfuse worker | 700 | 1 GiB | 2 GiB |
| LiteLLM | 475 | 512 MiB | 2 GiB |
| PostgreSQL | 60 | 256 MiB | 2 GiB |
| RustFS | 100 | 256 MiB | 1 GiB |
| Valkey | 8 | 128 MiB | 2 GiB |
| Each exporter | unmeasured | 32 MiB | 128 MiB |
| RustFS initializer / S3 backup helper | unmeasured | 128 MiB | 2 GiB |

Long-lived ceilings total 19.375 GiB, plus the transient S3 helper (eight concurrent
metadata requests) and other backup helpers. Leave space for the kernel, Docker, page
cache and backups. ClickHouse has the largest share because query and merge memory grow
with load. ClickHouse is capped at 4 CPUs and LiteLLM at 2 CPUs. These are starting bounds,
not throughput measurements; host smoke and a production-sized drill must validate them.

Valkey's application bound is `LG_VALKEY_MAXMEMORY=1gb`; its container ceiling is
`LG_VALKEY_MEM_LIMIT=2g`. The difference covers allocator overhead, AOF buffers and
copy-on-write during rewrites. Increase both together when raising queue capacity.
`noeviction` rejects writes at the application bound. Watch
`redis_memory_used_bytes / redis_memory_max_bytes` and container RSS; the former excludes
some overhead relevant to OOM.

## OOM and disk-full behavior

| Service | OOM / memory pressure | Disk full / recovery |
| --- | --- | --- |
| Caddy | Requests fail while the container restarts. | Certificate/config writes can fail; renewals fail until space is available. Preserve certificate volumes. |
| LiteLLM | In-flight calls can fail; restart restores service, callers must handle ambiguous completion outcomes. | PostgreSQL spend/key writes and Docker logging can fail. Restore capacity and inspect failed requests/accounting. |
| Langfuse web | UI and ingestion requests fail during restart. | Database/object writes fail. Free space and retry rejected ingestion from its producer. |
| Langfuse worker | Active work can stall/retry after restart; inspect failed BullMQ jobs and observation counts. | Store writes fail, retries increase and the queue grows. Restore store capacity before resuming ingestion. |
| ClickHouse | Queries may first hit server memory limits; a cgroup OOM aborts work and restarts the server. | Inserts, merges and backup writes fail. Expand storage or delete through Langfuse's supported lifecycle; never remove database parts manually. |
| PostgreSQL | A killed backend can force crash recovery; a killed server restarts and replays WAL. | Archive failure retains WAL locally; a full data disk stops writes and can stop the server. Add space, repair archiving and verify progress. Never delete live `pg_wal`. |
| RustFS | PUT/GET requests fail during restart; verify interrupted uploads. | PUTs fail and partial work may remain. Expand storage and retry failed uploads; never remove internal object-store files. |
| Valkey | Above maxmemory, `noeviction` rejects writes; cgroup OOM restarts from AOF, with possible loss since the last every-second fsync. | AOF writes fail and new writes may be rejected. Stop producers, restore space and inspect persistence status before restarting/retrying. |
| Exporters | Scrapes fail until restart; application state is unaffected. | Read-only containers have no durable files; Docker log disk pressure or unavailable stores makes telemetry fail. |
| S3 helper | The Checkpoint fails and retains incomplete artifacts; fenced services resume. | A full backup mount aborts the Checkpoint. Free/expand storage and rerun; failed runs do not prune. |

Docker restarts exited long-lived containers with `unless-stopped`; an unhealthy status
alone does not restart them. Check `docker inspect` for `State.OOMKilled`, container logs,
`docker stats --no-stream`, `df -h` and `df -i`. During store failure, stop ingestion to
avoid compounding the backlog, repair capacity, then verify database writes, archiving,
queue progress and a fresh Checkpoint. A process restart cannot repair a full disk.

## Growth and signals

Retention setup in [maintenance](maintenance.md) is mandatory before production ingestion.
Size ClickHouse and RustFS using measured bytes per request including prompt/response
payloads. The backup mount needs every retained Checkpoint plus one in progress, WAL since
the oldest retained base backup, and incomplete sets. Seven daily Checkpoints can require
more than seven times the live data size. Measure before choosing a disk.

Verify `redis_up{job="llm-gateway-valkey"} == 1`,
`redis_memory_used_bytes{job="llm-gateway-valkey"} / redis_memory_max_bytes{job="llm-gateway-valkey"}`,
and [archive/Checkpoint queries](backup.md). Alert above 80% Valkey maxmemory, below 15%
free disk bytes or inodes, on scrape failure and Checkpoint age over 26 hours. Check
ClickHouse merge backlog and Langfuse latency separately; this slice adds no ClickHouse
exporter or merge alert. A configured scrape and tested real receiver in the observability
stack are required before relying on alerts.
