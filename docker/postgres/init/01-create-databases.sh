#!/usr/bin/env bash
# Runs once on an empty cluster: one role and database each for LiteLLM and Langfuse.
set -euo pipefail

psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" \
  --set=litellm_password="$LITELLM_DB_PASSWORD" \
  --set=langfuse_password="$LANGFUSE_DB_PASSWORD" <<'SQL'
SELECT format('CREATE ROLE litellm LOGIN PASSWORD %L', :'litellm_password')
WHERE NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'litellm')\gexec
SELECT 'CREATE DATABASE litellm OWNER litellm'
WHERE NOT EXISTS (SELECT FROM pg_database WHERE datname = 'litellm')\gexec
SELECT format('CREATE ROLE langfuse LOGIN PASSWORD %L', :'langfuse_password')
WHERE NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'langfuse')\gexec
SELECT 'CREATE DATABASE langfuse OWNER langfuse'
WHERE NOT EXISTS (SELECT FROM pg_database WHERE datname = 'langfuse')\gexec
SQL
