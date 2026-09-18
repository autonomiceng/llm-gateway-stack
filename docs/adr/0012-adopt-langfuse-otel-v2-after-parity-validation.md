---
status: accepted
---

# Adopt Langfuse OTel v2 after parity validation

Use LiteLLM's OpenTelemetry v2 `langfuse_otel` preset as the target observability path
and capture prompt and response content on spans so self-hosted Langfuse retains its
expected debugging experience. Promote the switch into the Validated Release Lane only
after end-to-end tests cover chat, streaming, tools, failures, cache hits, costs,
identity and session metadata, tags, and trace continuation. Do not run the native and
OTel callbacks together after migration because that creates duplicate traces. If the
pinned release fails parity, retain the native callback and track the upstream gap.
Changing trace hierarchy or metadata is an Experience Change.

Amended 2026-09-17: Langfuse 4 removed the legacy ingestion API, so `langfuse_otel` is the only path. The smoke contract asserts that a plain and a streaming completion each produce a Langfuse observation with the caller's user id; tools, costs, sessions, tags, cache hits and trace continuation are not asserted yet and are tracked as a later slice.
