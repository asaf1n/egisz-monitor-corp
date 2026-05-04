#!/bin/bash
set -euo pipefail

_script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=include/mb_list.sh
. "${_script_dir}/include/mb_list.sh"

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

# METABASE_FORCE_PROVISION=auto раньше пропускал setup-dashboards при совпадении *числа* дашбордов с JSON в образе.
# Тогда Metabase application DB сохраняла старые native SQL — исправления в metabase_dashboards/*.json не подхватывались
# до METABASE_FORCE_PROVISION=true или DROP БД metabase. Сверяем якорный фрагмент эталона из образа с карточкой по API.
# Якорь — карточка «Последние операции» на дашборде 01 (оперативный мониторинг), а не «управленческая» сводка на 05.
corp_mb_native_sql_anchor_matches_image() {
  local token="$1"
  local ref_json="/app/metabase_dashboards/01_operational.json"
  local anchor='v_egisz_transactions_enriched_ui.* FROM public.v_egisz_transactions_enriched_ui'
  local card_name="Последние операции"
  local cards_json cid q

  if [ ! -f "${ref_json}" ]; then
    return 1
  fi
  if ! jq -e --arg a "${anchor}" --arg n "${card_name}" '.cards[] | select(.name == $n) | .dataset_query.native.query | contains($a)' "${ref_json}" >/dev/null 2>&1; then
    return 1
  fi

  cards_json="$(curl -sS "${MB_URL}/api/card" -H "X-Metabase-Session: ${token}" 2>/dev/null || echo '{}')"
  cid="$(echo "${cards_json}" | mb_list | jq -r --arg n "${card_name}" '[.[] | select(.name == $n) | select((.archived == false) or (.archived == null)) | .id] | max // empty')"
  if [ -z "${cid}" ] || [ "${cid}" = "null" ]; then
    return 1
  fi
  q="$(curl -sS "${MB_URL}/api/card/${cid}" -H "X-Metabase-Session: ${token}" | jq -r '.dataset_query.native.query // empty')"
  if [ -z "${q}" ]; then
    return 1
  fi
  case "${q}" in
    *"${anchor}"*) return 0 ;;
    *) return 1 ;;
  esac
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

# Идемпотентный provisioning: setup-dashboards.sh делает wipe личной коллекции и полный
# реимпорт — после переименования дашбордов в UI старый детект по префиксу «01 » в имени
# давал 0 и запускал провижининг заново (restart-metabase / rollout). Считаем дашборды
# в personal / is_personal коллекциях, с пагинацией GET /api/dashboard.
# Принудительная перезаливка: METABASE_FORCE_PROVISION=true или deploy/reset-deploy (DROP/CREATE БД).
EXPECTED_DASHBOARDS=0
if [ -d /app/metabase_dashboards ]; then
  for _f in /app/metabase_dashboards/*.json; do
    [ -f "${_f}" ] && EXPECTED_DASHBOARDS=$((EXPECTED_DASHBOARDS + 1)) || true
  done
fi

ME_JSON="$(curl -sS "${MB_URL}/api/user/current" -H "X-Metabase-Session: ${SESSION_TOKEN}")"
ROOT_ID="$(echo "${ME_JSON}" | jq -r '.personal_collection_id // empty')"
PERSONAL_IDS_JSON='[]'
if [ -n "${ROOT_ID}" ] && [ "${ROOT_ID}" != "null" ]; then
  PERSONAL_IDS_JSON="$(curl -sS "${MB_URL}/api/collection" -H "X-Metabase-Session: ${SESSION_TOKEN}" \
    | mb_list \
    | jq -c --arg rid "${ROOT_ID}" '
        (
          [.[] | select((.is_personal == true) or ((.id | tostring) == ($rid | tostring))) | .id | tostring]
          + [($rid | tostring)]
        ) | unique
      ')"
fi
PERSONAL_IDS_JSON="${PERSONAL_IDS_JSON:-[]}"

EXISTING_DASHBOARDS=0
_limit=200
_offset=0
_first_page_first_id=""
while true; do
  _page="$(curl -sS "${MB_URL}/api/dashboard?limit=${_limit}&offset=${_offset}" \
    -H "X-Metabase-Session: ${SESSION_TOKEN}" 2>/dev/null || echo '{}')"
  _arr="$(echo "${_page}" | mb_list)"
  _n="$(echo "${_arr}" | jq 'length')"
  if [ "${_n:-0}" -eq 0 ]; then
    break
  fi
  _first_id="$(echo "${_arr}" | jq -r '.[0].id // empty')"
  if [ "${_offset}" -gt 0 ] && [ -n "${_first_page_first_id}" ] && [ "${_first_id}" = "${_first_page_first_id}" ]; then
    log_info "WARN: /api/dashboard pagination ignored by server; stop to avoid double-count."
    break
  fi
  if [ "${_offset}" -eq 0 ]; then
    _first_page_first_id="${_first_id}"
  fi
  _chunk="$(echo "${_arr}" | jq --argjson pids "${PERSONAL_IDS_JSON}" '
    [.[]
      | select((.archived == false) or (.archived == null))
      | select(.collection_id != null and ((.collection_id | tostring) | IN($pids[])))
    ] | length
  ')"
  _chunk="${_chunk:-0}"
  EXISTING_DASHBOARDS=$((EXISTING_DASHBOARDS + _chunk))
  if [ "${_n}" -lt "${_limit}" ]; then
    break
  fi
  _offset=$((_offset + _limit))
  if [ "${_offset}" -gt 20000 ]; then
    log_info "WARN: dashboard list offset >20000; stop counting."
    break
  fi
done
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
      if corp_mb_native_sql_anchor_matches_image "${SESSION_TOKEN}"; then
        log_info "Skipping provisioning: ${EXISTING_DASHBOARDS} dashboard(s) in personal scope vs ${EXPECTED_DASHBOARDS} JSON; карточка «Последние операции» (01) native SQL совпадает с якорем из образа."
        PROVISION_DASHBOARDS=0
      else
        log_info "Re-provisioning (auto): count OK but «Последние операции» в Metabase не совпадает с эталоном из образа (stale native SQL). Importing from /app/metabase_dashboards."
        PROVISION_DASHBOARDS=1
      fi
    else
      log_info "Provisioning dashboards: have ${EXISTING_DASHBOARDS} of ${EXPECTED_DASHBOARDS} expected EGISZ dashboards in Metabase."
      PROVISION_DASHBOARDS=1
    fi
    ;;
esac

if [ -x /app/setup-dashboards.sh ] && [ "${PROVISION_DASHBOARDS}" = "1" ]; then
  log_info "Waiting for DWH schema in Postgres (needed for dashboard provisioning)..."
  SCHEMA_SQL="SELECT COUNT(*) FROM information_schema.tables WHERE table_schema='public' AND table_name IN ('fact_egisz_transactions', 'v_egisz_transactions_enriched', 'v_egisz_transactions_enriched_ui', 'v_rpt_semd_archive_ui', 'etl_state');"
  SCHEMA_CHECK="0"
  # -At: одна строка без пробелов/переносов — иначе сравнение в bash может не сработать
  for _attempt in $(seq 1 120); do
    SCHEMA_CHECK="$(PGPASSWORD="${APP_DB_PASSWORD}" psql -h "${PGHOST}" -U "${APP_DB_USER}" -d "${APP_DB_NAME}" -Atc "${SCHEMA_SQL}" 2>/dev/null || true)"
    SCHEMA_CHECK="$(echo "${SCHEMA_CHECK}" | tr -d '[:space:]')"
    SCHEMA_CHECK="${SCHEMA_CHECK:-0}"
    if [ "${SCHEMA_CHECK}" -ge 5 ] 2>/dev/null; then
      break
    fi
    sleep 5
  done

  # Только при полном наборе витрин (включая v_rpt_semd_archive_ui для дашборда «06 Архив СЭМД»).
  # Раньше здесь было -ge 4: при COUNT=4 setup-dashboards.sh стартовал без архива, провижининг дашбордов мог обрываться на неполном наборе JSON.
  if [ "${SCHEMA_CHECK}" -ge 5 ]; then
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
    echo "[provision] Warning: Application database schema not fully initialized after wait (need fact + v_egisz_transactions_enriched + v_egisz_transactions_enriched_ui + v_rpt_semd_archive_ui + etl_state). Skipping dashboard provisioning. Run egisz-monitor apply-schema and restart Metabase."
  fi
elif [ "${PROVISION_DASHBOARDS}" = "1" ]; then
  log_info "Note: /app/setup-dashboards.sh is missing; provisioning skipped."
fi

DASHBOARD_ID="$(curl -s "${MB_URL}/api/dashboard" \
  -H "X-Metabase-Session: ${SESSION_TOKEN}" | jq -r '
    (if type == "array" then . elif (.data | type == "array") then .data else [] end)
    | ([.[] | select(.name == "01 Оперативный мониторинг и динамика")] | sort_by(.id) | last | .id)
      // ([.[] | select(.name == "01 Оперативный мониторинг")] | sort_by(.id) | last | .id)
      // ([.[] | select(.name == "02 Сервис, healthcheck и парсинг журнала")] | sort_by(.id) | last | .id)
      // ([.[] | select(.name | test("^02 Сервис"))] | sort_by(.id) | last | .id)
      // ([.[] | select(.name == "05 Управление СЭМД")] | sort_by(.id) | last | .id)
      // ([.[] | select(.name == "05 Управление и архив СЭМД")] | sort_by(.id) | last | .id)
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

log_info "Metabase provisioning finished successfully"
log_info "Дашборды: в корне персональной коллекции администратора Metabase (тот же пункт в сайдбаре)."

