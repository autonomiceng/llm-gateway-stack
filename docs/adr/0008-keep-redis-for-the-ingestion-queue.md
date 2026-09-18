---
status: superseded by ADR-0014
date: 2026-09-17
---

# Keep Redis for the ingestion queue

Stay on first-party Redis, moving to the current 8.x stable lane. The 2024 license
change (RSALv2/SSPLv1) motivated a planned Valkey swap, but Redis 8 restored an
open-source AGPLv3 option, and for internal non-redistributed use that driver is gone;
the swap would have added a cross-vendor data-format rehearsal and a second upstream to
track with no feature gain. Langfuse uses this service as a durable ingestion queue,
not a disposable cache: persistence, `noeviction`, authentication, bounded memory, and
queue-recovery tests remain required, and 8.x keeps first-party RDB compatibility with
existing 7.x data. Reopen Valkey only if distribution or licensing stakes return.
