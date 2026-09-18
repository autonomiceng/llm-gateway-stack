---
status: superseded by ADR-0014
date: 2026-09-17
---

# Separate release, installation, and custom pins

Keep the tracked Release Manifest separate from operator configuration and from the
per-installation upgrade checkpoint state. Operators may apply Custom Pins for the
stateless applications LiteLLM, Langfuse, and Prometheus, and for the Stack Gateway,
through an ignored override file; the proven use case is rolling one application back
to a previous validated version when the selected release ships a bug. Pins are
resolved to tag plus digest, persist across upgrades, and appear in `./stack status`;
the reason is recorded as a comment in the override file rather than through an
acknowledgement ceremony. Installation Version State also records immutable
tag-plus-digest references, but it is an advisory checkpoint rather than a plain
Compose input. The Release Manifest, transitional legacy `.env` settings, Custom
Pins, and normal invoking-shell precedence determine the effective Compose version.
Persistent-store downgrades remain blocked and require recovery workflows.
Plain Compose is not gated on installation state: an interrupted upgrade is resumed by
rerunning `./stack upgrade`, which continues from Installation Version State without
repeating completed data migrations. `./stack` provides `upgrade`, `status`,
`migrate-env`, and Custom Pin management; `./stack status` reports mid-upgrade
checkpoint state and warns when effective versions diverge from the manifest.
