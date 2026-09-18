---
status: superseded by ADR-0014 (direct application ports and path redirects are gone; the static Caddy stays)
date: 2026-09-17
---

# Use a static Caddy gateway

Replace nginx with the official Caddy image and a static Caddyfile so the Stack Gateway
can serve Local Mode and hostname-based TLS Mode with automatic HTTPS. Do not use
`caddy-docker-proxy`: the topology is static and does not justify Docker API access.
Preserve the existing HTTP entry port and URLs, retain direct LiteLLM and Langfuse ports
as deprecated Local Mode compatibility paths for one release, and rely on the previous
stateless nginx release as the rollback path.
