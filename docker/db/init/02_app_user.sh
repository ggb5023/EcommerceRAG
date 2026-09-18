#!/usr/bin/env bash
set -euo pipefail
export ECR_APP_PASSWORD
ECR_APP_PASSWORD=$(cat /run/secrets/pg_app_password)
psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" <<'SQL'
\getenv app_password ECR_APP_PASSWORD
CREATE ROLE rag_app LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION PASSWORD :'app_password';
GRANT CONNECT ON DATABASE rag TO rag_app;
REVOKE CREATE ON SCHEMA public FROM PUBLIC;
GRANT USAGE ON SCHEMA public TO rag_app;
SQL
unset ECR_APP_PASSWORD
