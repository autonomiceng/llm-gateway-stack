# Change map

Read only the section for the files you touch, then run the gates in CONTRIBUTING.md.

## compose.yaml or an image pin

Default images use `${LG_COMPONENT_IMAGE:-tag@sha256}`; preserve the full-reference override
when changing a pin. Empty overrides select the default. A version bump is a Renovate PR or a hand edit that
resolves the digest with `docker buildx imagetools inspect <ref> --format '{{.Manifest.Digest}}'`.
Either way the smoke contract runs before merge. Langfuse web and worker move together.
Majors of Postgres, ClickHouse and Langfuse are one-way for data: read
`docs/operations/maintenance.md` first and take a Checkpoint before applying them anywhere real.
Only Caddy publishes ports. Only Caddy and the two datastore exporters join the platform network. No `env_file`.
`scripts/validate.sh` enforces all three.

## .env.example or scripts/bootstrap.py

The template lists every operator setting with one comment line above it and no secrets.
Bootstrap generates secrets it does not find and never rewrites a value it finds. Anything
that reads or writes `.env` keeps every unmanaged line as it was and appends, never edits. Tests in
`tests/test_bootstrap.py` use the fake runner; never call Docker from a unit test.

## docker/caddy

One Caddyfile for local, public and proxy modes; the template selects the matching small
Compose override. Test a change with `caddy validate` in all modes (validate.sh does this)
and the isolated gateway contract in `scripts/smoke-access.py` (smoke.sh runs it).
The console under
`docker/caddy/console/` is static: no build step, no framework, no continuous animation.
Health paths on the root hostname are the only paths that reach an app without its hostname.

## config.yaml or docker/litellm

LiteLLM reports to Langfuse through `langfuse_otel` only. Never add the legacy `langfuse`
callback beside it; that duplicates traces. Response reuse stays opt-in (ADR-0011).

## docker/postgres

Init scripts run once, on an empty cluster. The data directory is a host path mounted at
`/var/lib/postgresql`; the image places the cluster under `18/docker` inside it. Never mount
an older cluster into a newer major and call it an upgrade.

## Langfuse, ClickHouse, RustFS, Valkey

Langfuse owns its ClickHouse schema; no local DDL, no backfill jobs, no assumption that
ClickHouse can be rebuilt from Postgres or objects. RustFS holds events, media and exports;
a store change copies objects and verifies them, it never points a new store at old files.
Valkey is a durable queue: keep `noeviction`, AOF, authentication and the memory bound.

## docs/adr

Add an ADR only for a decision that is hard to reverse, surprising, and a real trade-off.
Supersede by adding front matter to the old file and naming it in the new one.
