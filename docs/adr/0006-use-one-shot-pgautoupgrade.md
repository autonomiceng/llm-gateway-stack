---
status: superseded by ADR-0014
date: 2026-09-17
---

# Use one-shot pgautoupgrade for PostgreSQL majors

PostgreSQL major upgrades use a pinned `pgautoupgrade` image in one-shot mode, then
return to the pinned Docker Official PostgreSQL image for steady-state operation. The
host data directory is mounted at `/var/lib/postgresql` so old and new clusters share a
filesystem and `pg_upgrade --link` can avoid copying the database; the operator may
confirm a recovery point or explicitly choose an Unprotected Upgrade.
