#!/usr/bin/env bash
# May also be run explicitly when adding monitoring to an existing cluster.
set -euo pipefail
psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname postgres \
  --set=monitor_password="$LG_POSTGRES_EXPORTER_PASSWORD" <<'SQL'
SELECT format('CREATE ROLE lg_monitor LOGIN PASSWORD %L', :'monitor_password')
WHERE NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'lg_monitor')\gexec
GRANT pg_monitor TO lg_monitor;
ALTER ROLE lg_monitor SET default_transaction_read_only = on;
SQL
