---
status: superseded by ADR-0014
date: 2026-09-17
---

# Retain MinIO as a compatibility bridge

Keep MinIO as the bundled object store for the current upgrade program because Langfuse
officially tests it and replacing persistent storage would add unacceptable risk to a
Safe Upgrade. Treat it as a compatibility bridge rather than a permanent strategic
choice: keep it private by default, make officially supported external S3 providers
first-class, and add an S3 contract test. Reopen replacement when Langfuse officially
supports another self-hosted store, RustFS reaches GA and passes that contract, or an
unpatched MinIO vulnerability forces action; any retained image remains pinned by a
validated version or digest. The real installation runs `cgr.dev/chainguard/minio` on a
floating `latest`; Stack Adoption resolves it to the pinned official image at an
equal-or-newer MinIO release; object-store downgrades are never part of adoption.
