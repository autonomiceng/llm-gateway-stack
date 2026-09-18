---
status: superseded by ADR-0014
date: 2026-09-17
---

# Provide local and TLS access modes

The Reference Stack boots in Local Mode without DNS or public certificates and requires
an explicit switch to TLS Mode for hostname-based automatic HTTPS through Caddy. Keeping
these modes distinct makes fresh installation self-contained and prevents public
exposure by default without forcing production operators back to raw ports or redirects.
