---
status: accepted
date: 2026-09-19
---

# Derive access from three modes and send runtime logs to journald

Amends ADR-0014's single-file and access-mode decisions. Local Mode serves HTTP and
private-CA HTTPS simultaneously, without redirects or HSTS; Public Mode uses ACME;
Proxy Mode delegates TLS to Platform Edge. The mode derives listener and issuer settings,
while the canonical application scheme remains independently configurable. Each Caddy
owns its existing certificate volumes; Proxy Mode needs no shared CA or private key.

Compose cannot conditionally omit one published port. The environment template selects
one small mode override so Proxy Mode publishes only HTTP; service topology and all image
pins remain in the base file. This requires Compose 2.24.4 or newer for `!override`.
There is no legacy access mode or migration layer.

Linux journald is the explicit deployment default, with Docker's file cache disabled.
This trades automatic portability for one local runtime log path independent of Alloy
and remote availability. Host journal retention remains host policy. Caddy access logs
use redacted JSON on stdout, runtime logs use stderr; upstream application file logging
is disabled. Product records, recovery state and protected backup diagnostics remain
persistent. The portability escape hatch is documented in the logging runbook.
