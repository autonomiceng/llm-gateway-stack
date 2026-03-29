#!/usr/bin/env bash
set -euo pipefail

psql \
  -v ON_ERROR_STOP=1 \
  --username "${POSTGRES_USER}" \
  --dbname "${POSTGRES_DB}" \
  --set=litellm_db_name="${LITELLM_DB_NAME}" \
  --set=litellm_db_user="${LITELLM_DB_USER}" \
  --set=litellm_db_password="${LITELLM_DB_PASSWORD}" \
  --set=langfuse_db_name="${LANGFUSE_DB_NAME}" \
  --set=langfuse_db_user="${LANGFUSE_DB_USER}" \
  --set=langfuse_db_password="${LANGFUSE_DB_PASSWORD}" <<'SQL'
SELECT format('CREATE ROLE %I LOGIN PASSWORD %L', :'litellm_db_user', :'litellm_db_password')
WHERE NOT EXISTS (SELECT FROM pg_roles WHERE rolname = :'litellm_db_user')\gexec

SELECT format('CREATE DATABASE %I OWNER %I', :'litellm_db_name', :'litellm_db_user')
WHERE NOT EXISTS (SELECT FROM pg_database WHERE datname = :'litellm_db_name')\gexec

SELECT format('CREATE ROLE %I LOGIN PASSWORD %L', :'langfuse_db_user', :'langfuse_db_password')
WHERE NOT EXISTS (SELECT FROM pg_roles WHERE rolname = :'langfuse_db_user')\gexec

SELECT format('CREATE DATABASE %I OWNER %I', :'langfuse_db_name', :'langfuse_db_user')
WHERE NOT EXISTS (SELECT FROM pg_database WHERE datname = :'langfuse_db_name')\gexec
SQL
