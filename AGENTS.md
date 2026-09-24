# llm-gateway-stack

A self-hosted LLM gateway: LiteLLM for routing, keys, budgets and caching; Langfuse for traces and evals; Postgres, ClickHouse, an S3-compatible object store and a Redis-compatible queue underneath; Caddy in front. One Docker Compose project, one host, production data.

Read before changing anything: `CONTEXT.md` (vocabulary, use these words), `docs/adr/` (decisions and why), `docs/agents/change-map.md` (which files a change touches and how to validate it). A change that contradicts an ADR is declared, never made quietly.

## Ways to hurt yourself

1. **Killing by pattern.** Never `pkill -f`, `pgrep | kill`, or `kill` a PID you found by matching a name, path, or worktree string. Your own agent process has this worktree's path in its argv, and this machine runs several other dev servers at once. Kill only a PID you captured at spawn, or the owner of your port from `ss -H -ltnp` after confirming `/proc/<pid>/cwd` is your worktree.
2. **Touching data you cannot rebuild.** Langfuse owns its ClickHouse schema and migrations. Never add local DDL or backfill jobs, never delete a volume or a Postgres data directory unless the user asked for data loss by name, and never mount an old Postgres major into a new image and call it an upgrade.
3. **Mutating the running stack to look at it.** Read files and `docker compose config` to inspect. `up`, `down`, `restart` and `pull` are changes and need the user's word.

## Communication

Short, direct, precise, industry standard language. No "not X, but Y", no em-dashes. State the result, then the evidence.

## Commits

- [Conventional Commits](https://conventionalcommits.org): `<type>(scope): <description>`, for example `feat(compose): publish only the gateway`.
- "Co-Authored-By:" should be set to "Various Models". Do not claim work performed by other models / subagents.
- Never commit `.env`. `.env.example` is the template. Never print secrets.

## Documentation

Most changes need no documentation change. Update `CONTEXT.md` when a term changes meaning, and add an ADR only for a decision that is hard to reverse, surprising, and a real trade-off.

## Plans and scratch

Never commit plans, research notes, or agent scratch. `.scratch/`, `.agents/`, `.devloop/` are gitignored. Local, untracked issues and specs live in `.scratch/<feature>/` as described in `docs/agents/issue-tracker.md`.

## Delegation

Model choice and the brief templates every delegation carries: `docs/agents/model-routing.md`.

## Where things live

- `compose.yaml` - the whole stack. Default images are pinned inline as tag plus digest, with full-reference `LG_*_IMAGE` overrides; default versions are written only here.
- `.env.example` - every operator setting, one comment line each, no secrets. `scripts/bootstrap.py` renders `.env` with generated secrets and starts the stack.
- `config.yaml` - LiteLLM proxy config. `docker/litellm/` holds its callback code.
- `docker/caddy/Caddyfile` - one file for Local and Public Mode; `docker/caddy/console/` is the static Stack Console.
- `docker/postgres/init/` - first-boot SQL. Runs only on an empty cluster.
- `scripts/` - `bootstrap.py` (also writes the Status Document, `data/console/status.json`), `validate.sh` (static gates, what CI runs), `smoke.sh` (the Smoke Contract, boots a disposable project), `backup.sh` and `restore.sh`, `retire-status-timer.sh` (one-time removal of the version 1 status timer).
- `tests/` - Python unittest with a fake runner; never calls Docker.
- `docs/DESIGN.md` the map, `docs/adr/` decisions, `docs/operations/` runbooks, `docs/agents/` guidance, `docs/conventions.md` what the four repos share.

Conventions: only Caddy publishes ports; only Caddy and the two datastore exporters join the `platform` network; each service lists its environment explicitly, no `env_file`; stack-owned settings are `LG_*`, upstream apps keep their names. Service and volume names are interfaces: renaming one needs a documented migration.

## Taste

- Compose is the product. Keep logic in the upstream apps and their config, not in glue scripts.
- The smallest change that is coherent on its own. No tests for documentation edits.
- Comments say why a setting is what it is and move when the setting moves.
- Prefer the upstream's documented default over a local workaround.
- If a rule here fights the task in front of you, say so and get a human sign-off before breaking it.

## Finish

Run the validation ladder in `docs/agents/change-map.md`. Report what ran, what could not run, and any operator action or compatibility break.
