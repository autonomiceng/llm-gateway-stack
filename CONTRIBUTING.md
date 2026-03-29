# Contributing

Thank you for helping improve this project.

## Getting started

1. Fork and clone the repository.
2. Copy environment template and generate secrets:

   ```bash
   cp .env.example .env
   ./scripts/init_env.py --force
   ```

3. Ensure the Postgres host data directory (see `POSTGRES_DATA_HOST_PATH` in `.env`) exists with suitable ownership for the Postgres container user.
4. Run `docker compose up -d` and follow [README.md](README.md) for Langfuse API keys and LiteLLM callbacks.

## Pull requests

- Keep changes focused and describe **what** changed and **why** in the PR description.
- Run `docker compose config` locally (with a `.env` present) after editing `docker-compose.yml`.
- Do not commit `.env` or other files containing real secrets.

## Code of conduct

Participants are expected to follow [CODE_OF_CONDUCT.md](CODE_OF_CONDUCT.md).

## AI-assisted work

If you use AI coding tools, see [LLM-DISCLOSURE.md](LLM-DISCLOSURE.md) for transparency expectations.

## Security

Report security issues per [SECURITY.md](SECURITY.md) rather than public issues when appropriate.
