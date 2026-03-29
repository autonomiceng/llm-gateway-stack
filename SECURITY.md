# Security policy

## Supported versions

Security fixes are applied to the **default branch** of this repository. Image pins in `.env.example` are updated as part of routine maintenance; always use current pins or your own vetted versions in production.

This stack also depends on LiteLLM. LiteLLM had a public PyPI supply-chain compromise in releases `1.82.7` and `1.82.8` in March 2026. The default image pin in this repository is intentionally below those affected releases, but anyone overriding the LiteLLM pin or installing LiteLLM separately should verify package provenance and avoid the affected versions.

See [SECURITY-NOTICE.md](SECURITY-NOTICE.md) for a short disclosure summary and source links.

## Reporting a vulnerability

If you believe you have found a security vulnerability:

1. **Do not** open a public GitHub issue with exploit details.
2. Contact the maintainers **privately** by opening a **GitHub Security Advisory** for this repository. If advisories are unavailable, use the private contact route listed on the repository owner or maintainer GitHub profile.

Please include:

- A short description of the issue and its impact
- Steps to reproduce (proof-of-concept if possible)
- Affected components (e.g. LiteLLM, Langfuse, Compose services)

We will aim to acknowledge receipt and coordinate a fix timeline. Thank you for responsible disclosure.

## Hardening reminders

Operational guidance (bind IPs, secrets, bootstrap credentials) lives in [README.md](README.md) under **Security Notes**.
