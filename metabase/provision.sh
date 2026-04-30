#!/bin/bash
set -euo pipefail
# Администратор Metabase: из k8s Secret metabase-admin → METABASE_ADMIN_EMAIL / METABASE_ADMIN_PASSWORD (репозиторий: admin@egisz.local / egisz).

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
SCHEMA_CHECK=0

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

log_info "Authenticating in Metabase..."
SESSION_TOKEN="$(curl -s -X POST "${MB_URL}/api/session" \
  -H "Content-Type: application/json" \
  -d "{\"username\":\"${ADMIN_EMAIL}\",\"password\":\"${ADMIN_PASSWORD}\"}" | jq -r '.id')"

if [ -z "${SESSION_TOKEN}" ] || [ "${SESSION_TOKEN}" = "null" ]; then
  echo "Failed to authenticate in Metabase"
  exit 1
fi

# Идемпотентный provisioning: setup-dashboards.sh при каждом старте пересоздаёт дашборды
# с новыми id'ами — это ломает закладки в браузере и ссылки в документации после rollout
# restart. Вместо этого считаем количество существующих "EGISZ" дашбордов (имя начинается
# с "01 ".."11 ") и пропускаем provisioning, если он уже выполнен. Принудительная пере-
# заливка — env METABASE_FORCE_PROVISION=true или Action reset-metabase (DROP/CREATE БД).
EXPECTED_DASHBOARDS=0
if [ -d /app/metabase_dashboards ]; then
  for _f in /app/metabase_dashboards/*.json; do
    [ -f "${_f}" ] && EXPECTED_DASHBOARDS=$((EXPECTED_DASHBOARDS + 1)) || true
  done
fi

EXISTING_DASHBOARDS="$(curl -sS "${MB_URL}/api/dashboard" \
  -H "X-Metabase-Session: ${SESSION_TOKEN}" \
  | jq -r '
      (if type == "array" then . elif (.data | type == "array") then .data else [] end)
      | [.[] | select(.archived != true) | select(.name | test("^[0-9][0-9] "))] | length
    ' 2>/dev/null || echo 0)"
EXISTING_DASHBOARDS="${EXISTING_DASHBOARDS:-0}"

PROVISION_DASHBOARDS=1
FORCE_FLAG="${METABASE_FORCE_PROVISION:-auto}"
case "${FORCE_FLAG}" in
  true|1|yes|on)
    log_info "METABASE_FORCE_PROVISION=${FORCE_FLAG}: dashboards will be re-provisioned (existing IDs will change)"
    PROVISION_DASHBOARDS=1
    ;;
  false|0|no|off|never)
    log_info "METABASE_FORCE_PROVISION=${FORCE_FLAG}: skipping dashboard provisioning unconditionally"
    PROVISION_DASHBOARDS=0
    ;;
  auto|*)
    if [ "${EXPECTED_DASHBOARDS}" -gt 0 ] && [ "${EXISTING_DASHBOARDS}" -ge "${EXPECTED_DASHBOARDS}" ]; then
      log_info "Skipping provisioning: ${EXISTING_DASHBOARDS}/${EXPECTED_DASHBOARDS} EGISZ dashboards already in Metabase application DB. Set METABASE_FORCE_PROVISION=true (or run 'start.ps1 -Action reset-metabase') to re-create."
      PROVISION_DASHBOARDS=0
    else
      log_info "Provisioning dashboards: have ${EXISTING_DASHBOARDS} of ${EXPECTED_DASHBOARDS} expected EGISZ dashboards in Metabase."
      PROVISION_DASHBOARDS=1
    fi
    ;;
esac

if [ -x /app/setup-dashboards.sh ] && [ "${PROVISION_DASHBOARDS}" = "1" ]; then
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
    echo "[provision] Warning: Application database schema not fully initialized after wait (need fact + v_egisz_transactions_enriched + v_egisz_transactions_enriched_ui + etl_state). Skipping dashboard provisioning. Run egisz-monitor apply-schema and restart Metabase."
  fi
elif [ "${PROVISION_DASHBOARDS}" = "1" ]; then
  log_info "Note: /app/setup-dashboards.sh is missing; provisioning skipped."
else
  # Используется ниже verify-corp-stack.sh для гейтинга проверки.
  SCHEMA_CHECK="${EXISTING_DASHBOARDS}"
fi

DASHBOARD_ID="$(curl -s "${MB_URL}/api/dashboard" \
  -H "X-Metabase-Session: ${SESSION_TOKEN}" | jq -r '
    (if type == "array" then . elif (.data | type == "array") then .data else [] end)
    | ([.[] | select(.name == "01 Оперативный мониторинг")] | sort_by(.id) | last | .id)
      // ([.[] | select(.name == "02 Сервис интеграции")] | sort_by(.id) | last | .id)
      // ([.[] | select(.name == "03 Ошибки и разбор")] | sort_by(.id) | last | .id)
      // empty
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

# Проверку не гоняем, если схему так и не дождались — иначе ложный FAIL при первом старте до Job схемы.
if [ -x /app/verify-corp-stack.sh ]; then
  if [ "${SCHEMA_CHECK:-0}" -ge 4 ]; then
    log_info "Running full stack verify (Postgres + Metabase EGISZ dashboards)..."
    if ! /app/verify-corp-stack.sh; then
      echo "[provision] ERROR: verify-corp-stack.sh failed (см. вывод выше)"
      exit 1
    fi
  else
    log_info "Skipping verify: DWH schema not ready (dashboard provisioning was skipped)."
  fi
fi

log_info "Metabase provisioning finished successfully"
log_info "Дашборды: в корне персональной коллекции администратора Metabase (тот же пункт в сайдбаре)."

