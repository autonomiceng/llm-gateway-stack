---
status: superseded by ADR-0014
date: 2026-09-17
---

# Adopt the predecessor installation

The only real installation with data runs a predecessor Compose stack, not this
repository. The first release therefore treats Stack Adoption (migrating that
installation's data onto the Reference Stack) as the flagship migration path rather
than an in-place release-to-release upgrade. Adoption is rehearsed on copied data,
requires the same recovery-point confirmation as a Safe Upgrade, and starts from a
verified diff of the predecessor's services, volumes, environment keys, and data paths
against this stack; service-name or volume compatibility is never assumed.
