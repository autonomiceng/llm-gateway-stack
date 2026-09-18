# Stack conventions

How the four repos (llm-gateway-stack, agent-backplane, observability-stack and
platform-edge) are laid out and operated, so that deploying any of them feels the same.
This file, `docs/conventions.md`, is the shared reference.

## Repository

- `compose.yaml` at the root with `name:` set. Optional pieces are `profiles:`; overlay files
  only where a profile cannot express it. `docker compose up` with no flags starts the core.
- Images pinned inline as `image:tag@sha256`. Renovate proposes bumps; a human merges after
  the stack's smoke contract passes. No floating tags, no `latest`.
- `.env.example` lists every operator setting: one comment line, then the assignment. No
  secrets in the template; bootstrap generates them. Stack-owned settings carry a prefix
  (`LG_`, `BP_`, `OB_`, `PE_`); upstream applications keep their upstream variable names.
- `mise.toml` pins the toolchain. `scripts/` holds bootstrap, validate, smoke, backup and
  restore. `docs/DESIGN.md` is the map, `docs/adr/` the decisions, `CONTEXT.md` the glossary,
  `docs/operations/` the runbooks, `docs/agents/` the guidance for agents.
- `AGENTS.md` at the root, `CLAUDE.md` containing `@AGENTS.md`. CONTRIBUTING, SECURITY, PR
  and issue templates in the same shape across repos.
- Default branch `main`. Conventional Commits. `Co-Authored-By: Various Models`.
- `.scratch/`, `.agents/`, `.devloop/`, `.plans/` are gitignored. Plans never commit.

## Bootstrap contract

One command from clone to running stack. It locks the env file, generates missing secrets
(mode 0600, never rewrites a present value, keeps unmanaged lines byte for byte), refuses to
start when installation state exists and secrets are missing, creates the shared network,
runs `docker compose up --wait`, probes readiness, and prints the next step as JSON. Errors
are one JSON line on stderr. Exit codes: 0 ready, 1 refused, 2 usage, 3 not ready.

## Network and ingress

- Each stack ships its own Caddy and publishes only Caddy. Everything else is private.
- One `PUBLIC_DOMAIN` per stack with fixed subdomains. Default `localhost`, plain HTTP on
  loopback. Public mode is scheme `https`, bind `0.0.0.0`, issuer `acme` or `internal`.
  Hostnames do not change between modes.
- On a shared host every stack joins the external Docker network `platform` with only its
  ingress target and metrics endpoints, under prefixed aliases (`lg-`, `bp-`, `ob-`, `pe-`).
  Datastores never join. The optional `platform-edge` project owns 80 and 443 and routes each
  hostname to a stack's Caddy over that network.

## Data and operations

- Datastore state is Compose volumes, except a Postgres host path where the operator chooses
  the disk. Volumes are named by role.
- A Checkpoint is one consistent backup set across every store plus a manifest of pins and
  configuration, taken with ingestion fenced. `backup.sh`, `restore.sh`, and a drill script
  that proves the pair. RPO and RTO are stated in `docs/operations/backup.md`. Encryption and
  off-host replication of the backup mount are the operator's job.
- Telemetry: container logs and each stack's `/metrics` go to the observability stack over
  the platform network. Domain data (LLM traces, backplane audit) stays where it lives.
- Health: each stack exposes health routes through Caddy for the console and bootstrap.
  Use container healthchecks where supported. Observability’s distroless backends are
  probed by Caddy’s aggregate healthcheck and bootstrap/smoke HTTP assertions.
  Public gateway probes may return only status; detailed responses require operator access.
- Versions bump through Renovate PRs; majors of stateful stores are labelled and applied only
  from a Checkpoint following `docs/operations/maintenance.md`.

## Agents

Model routing lives in each repo's `docs/agents/model-routing.md`, with repo-specific risk
paths and these shared roles: Claude Fable 5.1 orchestrates, designs, writes prose and UI, and gives the
final review of anything touching persistent data; gpt-6 high red-teams, reviews designs and
implements high-risk slices; gpt-5.6-sol medium does routine work. The verifier is from a
different family than the implementer when possible. Two workers per host at most.

## Stack-specific operations

Observability connects Caddy, Grafana and Alloy to the Platform Network. Its optional
`compose.s3.yaml` replaces backend storage mounts; bootstrap records the selection in
`COMPOSE_FILE`. Platform Edge owns shared ingress and certificate volumes; it has one
Caddy service and no datastore. Per-stack runbooks define their exact health routes,
checkpoint contents and recovery procedures.
