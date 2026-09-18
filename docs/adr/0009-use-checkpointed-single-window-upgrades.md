---
status: superseded by ADR-0014
date: 2026-09-17
---

# Use checkpointed single-window upgrades

Operators invoke one upgrade for one maintenance window: images and prerequisites are
prepared before ingress stops, persistent and application transitions run in dependency
order, and public access returns only after the full-stack smoke test. Internally, each
successful component checkpoint advances Installation Version State so a failed run can
resume without repeating completed data migrations.

An upgrade preserves explicit operator settings but applies new validated defaults where
the operator has not set an override. Before downtime, the tool lists every Experience
Change with its impact and rollback guidance and requires acknowledgement scoped to that
release; it does not support a permanent generic bypass.
