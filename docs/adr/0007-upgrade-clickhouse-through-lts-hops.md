---
status: accepted
---

# Upgrade ClickHouse through LTS hops

Fresh installs use the pinned newest ClickHouse LTS lane. No multi-hop migration from
23.8 is maintained: the 23.8 pin could never boot the repo's own DDL, so no installation
holds 23.8-era data. The one real installation ran a floating `latest` (26.1 at time of
decision); it is pinned by tag and digest out-of-band and upgrades offline, forward only,
to the newest LTS at or above its current version — persistent-store downgrades to an
older lane are not permitted. Langfuse owns its ClickHouse schema, so the local `events`
DDL/backfill workaround and its Compose migration service are removed; legacy tables are
cleaned up only through an explicit post-upgrade operator action.
