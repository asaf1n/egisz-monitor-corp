#!/bin/bash
set -euo pipefail

MB_URL="${MB_URL:-http://metabase:3000}"
ADMIN_EMAIL="${METABASE_ADMIN_EMAIL:-${METABASE_ADMIN_USER:-}}"
ADMIN_PASSWORD="${METABASE_ADMIN_PASSWORD:-}"
APP_DB_NAME="${APP_DATABASE_NAME:-egisz_reports}"
APP_DB_DISPLAY_NAME="${APP_DATABASE_DISPLAY_NAME:-EGISZ Corp DWH}"
APP_DB_USER="${APP_DATABASE_USER:-egisz}"
APP_DB_PASSWORD="${APP_DATABASE_PASSWORD:-egisz}"
PGHOST="${PGHOST:-postgres}"
SITE_NAME="${METABASE_SITE_NAME:-EGISZ Monitor Corp}"
PUBLIC_UUID_FILE="${METABASE_PUBLIC_UUID_FILE:-/shared/main-dashboard-public-uuid}"

log_info() {
  echo "[provision] $1"
}

if [ -z "${ADMIN_EMAIL}" ] || [ -z "${ADMIN_PASSWORD}" ]; then
  echo "Metabase admin credentials are not configured"
  exit 1
fi

log_info "Waiting for Metabase API at ${MB_URL}..."
until curl --output /dev/null --silent --fail "${MB_URL}/api/health"; do
  printf '.'
  sleep 5
done
echo

PROPERTIES="$(curl -s "${MB_URL}/api/session/properties")"
HAS_USER_SETUP="$(echo "${PROPERTIES}" | jq -r '."has-user-setup"')"

if [ "${HAS_USER_SETUP}" != "true" ]; then
  SETUP_TOKEN="$(echo "${PROPERTIES}" | jq -r '."setup-token"')"

  if [ -z "${SETUP_TOKEN}" ] || [ "${SETUP_TOKEN}" = "null" ]; then
    echo "Metabase setup token is missing"
    exit 1
  fi

  log_info "Bootstrapping Metabase admin user..."
  SETUP_PAYLOAD="$(jq -n \
    --arg token "${SETUP_TOKEN}" \
    --arg email "${ADMIN_EMAIL}" \
    --arg password "${ADMIN_PASSWORD}" \
    --arg siteName "${SITE_NAME}" \
    --arg dbName "${APP_DB_DISPLAY_NAME}" \
    --arg dbRealName "${APP_DB_NAME}" \
    --arg dbUser "${APP_DB_USER}" \
    --arg dbPassword "${APP_DB_PASSWORD}" \
    --arg pgHost "${PGHOST}" \
    '{
      token: $token,
      user: {
        first_name: "EGISZ",
        last_name: "Admin",
        email: $email,
        password: $password
      },
      database: {
        engine: "postgres",
        name: $dbName,
        details: {
          host: $pgHost,
          port: 5432,
          dbname: $dbRealName,
          user: $dbUser,
          password: $dbPassword,
          ssl: false,
          "tunnel-enabled": false,
          "advanced-options": false
        }
      },
      prefs: {
        site_name: $siteName,
        site_locale: "ru"
      }
    }')"

  RESPONSE="$(curl -s -w '\n%{http_code}' -X POST "${MB_URL}/api/setup" \
    -H "Content-Type: application/json" \
    -d "${SETUP_PAYLOAD}")"

  HTTP_CODE="$(echo "${RESPONSE}" | tail -n1)"
  BODY="$(echo "${RESPONSE}" | sed '$d')"

  if [ "${HTTP_CODE}" != "200" ]; then
    echo "Metabase setup failed with HTTP ${HTTP_CODE}"
    echo "${BODY}"
    exit 1
  fi
fi

if [ -x /app/setup-dashboards.sh ]; then
  log_info "Waiting for DWH schema in Postgres (needed for dashboard provisioning)..."
  SCHEMA_SQL="SELECT COUNT(*) FROM information_schema.tables WHERE table_schema='public' AND table_name IN ('fact_egisz_transactions', 'v_egisz_transactions_enriched', 'v_egisz_transactions_enriched_ui', 'etl_state');"
  SCHEMA_CHECK="0"
  # -At: одна строка без пробелов/переносов — иначе сравнение в bash может не сработать
  for _attempt in $(seq 1 120); do
    SCHEMA_CHECK="$(PGPASSWORD="${APP_DB_PASSWORD}" psql -h "${PGHOST}" -U "${APP_DB_USER}" -d "${APP_DB_NAME}" -Atc "${SCHEMA_SQL}" 2>/dev/null || true)"
    SCHEMA_CHECK="$(echo "${SCHEMA_CHECK}" | tr -d '[:space:]')"
    SCHEMA_CHECK="${SCHEMA_CHECK:-0}"
    if [ "${SCHEMA_CHECK}" -ge 4 ] 2>/dev/null; then
      break
    fi
    sleep 5
  done

  if [ "${SCHEMA_CHECK}" -ge 4 ]; then
    log_info "Application schema validated. Running dashboard provisioning..."
    ADMIN_EMAIL="${ADMIN_EMAIL}" \
    ADMIN_PASSWORD="${ADMIN_PASSWORD}" \
    DB_NAME="${APP_DB_NAME}" \
    DB_DISPLAY_NAME="${APP_DB_DISPLAY_NAME}" \
    DB_USER="${APP_DB_USER}" \
    DB_PASSWORD="${APP_DB_PASSWORD}" \
    METABASE_URL="${MB_URL}" \
    PGHOST="${PGHOST}" \
    PGPORT="5432" \
    /app/setup-dashboards.sh
  else
    echo "[provision] Warning: Application database schema not fully initialized after wait (need fact + v_egisz_transactions_enriched + v_egisz_transactions_enriched_ui + etl_state). Skipping dashboard provisioning. Run egisz-corp apply-schema and restart Metabase."
  fi
fi

log_info "Authenticating in Metabase..."
SESSION_TOKEN="$(curl -s -X POST "${MB_URL}/api/session" \
  -H "Content-Type: application/json" \
  -d "{\"username\":\"${ADMIN_EMAIL}\",\"password\":\"${ADMIN_PASSWORD}\"}" | jq -r '.id')"

if [ -z "${SESSION_TOKEN}" ] || [ "${SESSION_TOKEN}" = "null" ]; then
  echo "Failed to authenticate in Metabase"
  exit 1
fi

DASHBOARD_ID="$(curl -s "${MB_URL}/api/dashboard" \
  -H "X-Metabase-Session: ${SESSION_TOKEN}" | jq -r '
    [
      .[]
      | select(
          .name == "Оперативный мониторинг (Corp)"
          or .name == "Сервис интеграции (Corp)"
          or .name == "Ошибки и разбор (Corp)"
        )
    ]
    | sort_by(.id)
    | last
    | .id // empty
  ')"

if [ -n "${DASHBOARD_ID}" ]; then
  PUBLIC_UUID="$(curl -s "${MB_URL}/api/dashboard/${DASHBOARD_ID}" \
    -H "X-Metabase-Session: ${SESSION_TOKEN}" | jq -r '.public_uuid // empty')"

  if [ -z "${PUBLIC_UUID}" ] || [ "${PUBLIC_UUID}" = "null" ]; then
    PUBLIC_UUID="$(curl -s -X POST "${MB_URL}/api/dashboard/${DASHBOARD_ID}/public_link" \
      -H "Content-Type: application/json" \
      -H "X-Metabase-Session: ${SESSION_TOKEN}" \
      -d '{}' | jq -r '.uuid // empty')"
  fi

  if [ -n "${PUBLIC_UUID}" ] && [ "${PUBLIC_UUID}" != "null" ]; then
    mkdir -p "$(dirname "${PUBLIC_UUID_FILE}")"
    printf '%s' "${PUBLIC_UUID}" > "${PUBLIC_UUID_FILE}"
    log_info "Published dashboard UUID: ${PUBLIC_UUID}"
  fi
else
  rm -f "${PUBLIC_UUID_FILE}"
  log_info "No primary dashboard found for publishing; stale public UUID removed."
fi

log_info "Metabase provisioning finished successfully"

