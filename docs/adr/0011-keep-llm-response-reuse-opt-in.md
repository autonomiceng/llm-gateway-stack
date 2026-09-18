---
status: accepted
---

# Keep LLM response reuse opt-in

Enable operational caches by default and configure Valkey-backed LLM response caching
as a ready, observable capability, but keep response reuse explicitly opt-in per model
or request. The stack must enforce tenant-aware namespaces, include provider-specific
optional parameters in cache keys, and provide bounded-TTL recipes. Global response
reuse changes freshness, nondeterminism, sensitive-data retention, spend attribution,
and tracing semantics, so enabling it by default would be an Experience Change. Safe
Upgrade tooling must disclose Experience Changes before downtime and link their
migration and rollback guidance.
