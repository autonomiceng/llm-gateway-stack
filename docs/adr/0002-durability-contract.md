---
status: accepted
---

# Set the durability contract

Safe Upgrades target an RPO of zero: ingestion is stopped and the operator confirms that
a usable recovery point exists before persistent data is migrated. Tooling must warn
and document recovery options, but operators may explicitly choose an Unprotected
Upgrade; doing so waives the stack's durability and rollback claims. Unexpected host
failure has a default documented RPO target of 24 hours, which operators may improve
with more frequent or external backups; recovery is tested and documented but has no
guaranteed RTO.

Amended 2026-09-17: Safe and Unprotected Upgrade tooling is gone (ADR-0014). The recovery point before a persistent change is taken by `backup.sh`; the maintenance procedure in `docs/operations/maintenance.md` names the rollback boundary per store. RPO is the Checkpoint interval; the WAL archive supports manual point-in-time recovery by an expert. Daily successful off-host Checkpoints target 24 hours. A later Postgres point-in-time restore requires reconciliation with ClickHouse, objects and the queue.
