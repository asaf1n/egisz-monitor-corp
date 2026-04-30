#!/bin/bash
# Вызывается из пода postgres (env из Secret postgres-credentials).
# Старый PVC мог быть инициализирован с другим POSTGRES_USER — тогда роль egisz отсутствует,
# а Metabase/ETL подключаются как egisz → FATAL: role egisz does not exist.
set -euo pipefail

: "${POSTGRES_USER:?POSTGRES_USER missing}"
: "${POSTGRES_PASSWORD:?POSTGRES_PASSWORD missing}"
: "${POSTGRES_DB:?POSTGRES_DB missing}"

PW_ESC=$(printf '%s\n' "${POSTGRES_PASSWORD}" | sed "s/'/''/g")

run_local_postgres() {
  psql -U postgres -d postgres -v ON_ERROR_STOP=1 "$@"
}

run_tcp_app() {
  PGPASSWORD="${POSTGRES_PASSWORD}" psql -h 127.0.0.1 -p 5432 -U "${POSTGRES_USER}" -d postgres -v ON_ERROR_STOP=1 "$@"
}

if run_local_postgres -c "SELECT 1" >/dev/null 2>&1; then
  SUPER_MODE=local_postgres
elif run_tcp_app -c "SELECT 1" >/dev/null 2>&1; then
  SUPER_MODE=tcp_app
else
  echo "[ensure-postgres-app-role] ERROR: ни локально postgres, ни TCP как ${POSTGRES_USER}. Проверьте Secret и том." >&2
  exit 1
fi

super_sql() {
  if [ "${SUPER_MODE}" = "local_postgres" ]; then
    run_local_postgres "$@"
  else
    run_tcp_app "$@"
  fi
}

HAS_ROLE="$(super_sql -tAc "SELECT 1 FROM pg_roles WHERE rolname = 'egisz'" 2>/dev/null | tr -d '[:space:]' || true)"
HAS_ROLE="${HAS_ROLE:-0}"

if [ "${SUPER_MODE}" = "tcp_app" ] && [ "${POSTGRES_USER}" = "egisz" ] && [ "${HAS_ROLE}" = "1" ]; then
  echo "[ensure-postgres-app-role] Роль egisz уже есть (совпадает с POSTGRES_USER), синхронизирую пароль из Secret..."
  super_sql -c "ALTER ROLE egisz WITH LOGIN SUPERUSER PASSWORD '${PW_ESC}';"
elif [ "${HAS_ROLE}" = "1" ]; then
  echo "[ensure-postgres-app-role] Обновляю пароль роли egisz из Secret..."
  super_sql -c "ALTER ROLE egisz WITH LOGIN SUPERUSER PASSWORD '${PW_ESC}';"
else
  echo "[ensure-postgres-app-role] Создаю роль egisz (SUPER_MODE=${SUPER_MODE})..."
  super_sql -c "CREATE ROLE egisz LOGIN SUPERUSER PASSWORD '${PW_ESC}';"
fi

HAS_DB="$(super_sql -tAc "SELECT 1 FROM pg_database WHERE datname = '${POSTGRES_DB}'" | tr -d '[:space:]' || true)"
HAS_DB="${HAS_DB:-0}"
if [ "${HAS_DB}" != "1" ]; then
  echo "[ensure-postgres-app-role] Создаю БД ${POSTGRES_DB} (owner egisz)..."
  super_sql -c "CREATE DATABASE \"${POSTGRES_DB}\" OWNER egisz;"
else
  echo "[ensure-postgres-app-role] БД ${POSTGRES_DB} уже есть."
fi

echo "[ensure-postgres-app-role] OK"
