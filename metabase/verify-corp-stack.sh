#!/bin/bash
# Проверка витрины Postgres + наличия коллекции/дашбордов EGISZ в Metabase (запуск из пода Metabase).
# Логин API: METABASE_ADMIN_EMAIL / METABASE_ADMIN_PASSWORD из Secret metabase-admin (репозиторий: admin@egisz.local / egisz).
set -euo pipefail

MB_URL="${MB_URL:-http://localhost:3000}"
ADMIN_EMAIL="${METABASE_ADMIN_EMAIL:-${ADMIN_EMAIL:-}}"
ADMIN_PASSWORD="${METABASE_ADMIN_PASSWORD:-${ADMIN_PASSWORD:-}}"
PGHOST="${PGHOST:-postgres}"
PGPORT="${PGPORT:-5432}"
APP_DB="${APP_DATABASE_NAME:-egisz_reports}"
APP_USER="${APP_DATABASE_USER:-egisz}"
APP_PW="${APP_DATABASE_PASSWORD:-egisz}"

mb_list() {
  # Metabase 0.49+ обычно { "data": [...] }; реже обёртки — не даём ложный ND=0.
  jq -c '
    if type == "array" then .
    elif (.data | type == "array") then .data
    elif (.items | type == "array") then .items
    elif (.data | type == "object") and (.data.items | type == "array") then .data.items
    else [] end
  '
}

log() {
  echo "[verify] $*"
}

if [ -z "${ADMIN_EMAIL}" ] || [ -z "${ADMIN_PASSWORD}" ]; then
  log "FAIL: METABASE_ADMIN_EMAIL / METABASE_ADMIN_PASSWORD not set"
  exit 1
fi

log "Waiting for Metabase ${MB_URL}/api/health ..."
until curl --output /dev/null --silent --fail "${MB_URL}/api/health"; do
  sleep 3
done

log "Postgres: core DWH tables at ${PGHOST}:${PGPORT}/${APP_DB}"
export PGPASSWORD="${APP_PW}"
chk="$(psql -h "${PGHOST}" -p "${PGPORT}" -U "${APP_USER}" -d "${APP_DB}" -Atqc \
  "SELECT COUNT(*) FROM information_schema.tables WHERE table_schema='public' AND table_name IN ('fact_egisz_transactions','v_egisz_transactions_enriched','v_egisz_transactions_enriched_ui','etl_state');" \
  2>/dev/null || echo 0)"
chk="$(echo "${chk}" | tr -d '[:space:]')"
chk="${chk:-0}"
if ! [ "${chk}" -ge 4 ] 2>/dev/null; then
  log "FAIL: expected 4 core tables in public, got '${chk}' (apply schema Job / ETL host)"
  exit 1
fi
log "Postgres OK (${chk} matched)"

log "Metabase: login as ${ADMIN_EMAIL}"
TOKEN="$(curl -sS -X POST "${MB_URL}/api/session" \
  -H "Content-Type: application/json" \
  -d "{\"username\":\"${ADMIN_EMAIL}\",\"password\":\"${ADMIN_PASSWORD}\"}" | jq -r '.id // empty')"
if [ -z "${TOKEN}" ] || [ "${TOKEN}" = "null" ]; then
  log "FAIL: Metabase /api/session (wrong password or setup not finished)"
  exit 1
fi

log "Metabase: dashboards in admin personal collection (or any namespaced is_personal collection)"
ME_JSON="$(curl -sS "${MB_URL}/api/user/current" -H "X-Metabase-Session: ${TOKEN}")"
ROOT_ID="$(echo "${ME_JSON}" | jq -r '.personal_collection_id // empty')"
if [ -z "${ROOT_ID}" ] || [ "${ROOT_ID}" = "null" ]; then
  log "FAIL: personal_collection_id missing (cannot verify dashboards placement)"
  exit 1
fi

# provision.sh кладёт дашборды в namespaced collection «ЕГИСЗ Мониторинг сервиса интеграции»
# с is_personal=true (а не в дефолтную «Личная коллекция admin'а»). Поэтому считаем дашборды
# и в дефолтной personal collection ROOT_ID, и в любой коллекции с is_personal=true.
PERSONAL_IDS_JSON="$(curl -sS "${MB_URL}/api/collection" -H "X-Metabase-Session: ${TOKEN}" \
  | mb_list \
  | jq -c --arg rid "${ROOT_ID}" '
      [
        .[]
        | select((.is_personal == true) or ((.id | tostring) == ($rid | tostring)))
        | .id | tostring
      ] | unique
    ')"
PERSONAL_IDS_JSON="${PERSONAL_IDS_JSON:-[]}"

ITEMS="$(curl -sS "${MB_URL}/api/collection/${ROOT_ID}/items" -H "X-Metabase-Session: ${TOKEN}")"
ND="$(curl -sS "${MB_URL}/api/dashboard" -H "X-Metabase-Session: ${TOKEN}" \
  | mb_list \
  | jq -r --argjson pids "${PERSONAL_IDS_JSON}" '
      [.[] | select(.collection_id != null and ((.collection_id | tostring) | IN($pids[])))] | length
    ')"
ND="${ND:-0}"

if [ "${ND:-0}" -lt 1 ]; then
  # Fallback: items дефолтной personal collection — на случай старых деплоев, где провижин клал в admin's personal.
  ND="$(echo "${ITEMS}" | mb_list | jq -r '[.[] | select(((.model // "") | ascii_downcase) == "dashboard")] | length')"
  ND="${ND:-0}"
fi

EXPECTED=0
if [ -d /app/metabase_dashboards ]; then
  for _f in /app/metabase_dashboards/*.json; do
    [ -f "${_f}" ] && EXPECTED=$((EXPECTED + 1)) || true
  done
fi

if [ "${ND:-0}" -lt 1 ]; then
  log "FAIL: no dashboards in personal collection (dashcards PUT may have failed)"
  exit 1
fi
if [ "${EXPECTED}" -gt 0 ] && [ "${ND}" -lt "${EXPECTED}" ]; then
  log "FAIL: dashboards count ${ND} < expected ${EXPECTED} from /app/metabase_dashboards"
  exit 1
fi
if [ "${EXPECTED}" -gt 0 ] && [ "${ND}" -gt "${EXPECTED}" ]; then
  log "FAIL: dashboards count ${ND} > expected ${EXPECTED} (duplicate names or stale collections — run reset-deploy / wipe Metabase root)"
  exit 1
fi

log "Metabase OK (${ND} dashboard(s) in personal collection root, expected files=${EXPECTED}; saved questions from dashcards also appear in this list in UI)"
log "В UI: откройте «Персональная коллекция …» (collection_id=${ROOT_ID}) — дашборды на этой странице."

# Сверка числа карточек на управленческом дашборде с JSON в образе (ловит старый image :latest без пересборки).
EXEC_JSON="/app/metabase_dashboards/09_executive.json"
if [ -f "${EXEC_JSON}" ]; then
  EXP_CARDS="$(jq '.cards | length' "${EXEC_JSON}")"
  EXEC_NAME="$(jq -r '.name' "${EXEC_JSON}")"
  EXEC_DID="$(curl -sS "${MB_URL}/api/dashboard" -H "X-Metabase-Session: ${TOKEN}" \
    | mb_list \
    | jq -r --arg n "${EXEC_NAME}" --argjson pids "${PERSONAL_IDS_JSON}" '
        [.[] | select(.name == $n and (.collection_id != null) and ((.collection_id | tostring) | IN($pids[])))]
        | sort_by(.id) | last | .id // empty
      ')"
  if [ -z "${EXEC_DID}" ] || [ "${EXEC_DID}" = "null" ]; then
    EXEC_DID="$(echo "${ITEMS}" | mb_list | jq -r --arg n "${EXEC_NAME}" '.[] | select(((.model // "") | ascii_downcase) == "dashboard" and .name == $n) | .id' | head -n1)"
  fi
  if [ -z "${EXEC_DID}" ] || [ "${EXEC_DID}" = "null" ]; then
    log "FAIL: dashboard \"${EXEC_NAME}\" not in personal collection (provisioning may have failed)"
    exit 1
  fi
  DASH_JSON="$(curl -sS "${MB_URL}/api/dashboard/${EXEC_DID}" -H "X-Metabase-Session: ${TOKEN}")"
  GOT_CARDS="$(echo "${DASH_JSON}" | jq '
    if (.dashcards | type == "array") and ((.dashcards | length) > 0) then (.dashcards | length)
    elif (.ordered_cards | type == "array") then (.ordered_cards | length)
    else 0 end
  ')"
  GOT_CARDS="$(echo "${GOT_CARDS}" | tr -d '[:space:]')"
  GOT_CARDS="${GOT_CARDS:-0}"
  if ! [ "${GOT_CARDS}" -eq "${EXP_CARDS}" ] 2>/dev/null; then
    log "FAIL: \"${EXEC_NAME}\" has ${GOT_CARDS} dashcards, image JSON expects ${EXP_CARDS} — пересоберите образ Metabase (metabase_dashboards внутри образа) и перезапустите deployment."
    exit 1
  fi
  MAPS="$(echo "${DASH_JSON}" | jq '([ (.dashcards // .ordered_cards // [])[]? | (.parameter_mappings // []) | length ] | add) // 0')"
  MAPS="$(echo "${MAPS}" | tr -d '[:space:]')"
  MAPS="${MAPS:-0}"
  if ! [ "${MAPS}" -gt 0 ] 2>/dev/null; then
    log "FAIL: \"${EXEC_NAME}\": нет parameter_mappings на карточках (нативные фильтры не связаны). Перезалейте дашборды: METABASE_FORCE_PROVISION=true или deploy/reset-deploy (DROP/CREATE app DB)."
    exit 1
  fi
  log "Dashboard \"${EXEC_NAME}\" OK (${GOT_CARDS} dashcards, parameter_mappings=${MAPS})"
fi

if [ -x /app/smoke-metabase-ui.sh ]; then
  log "smoke-metabase-ui.sh (10 HTTP checks: filters, auto_apply, card query)"
  MB_URL="${MB_URL:-http://localhost:3000}" /app/smoke-metabase-ui.sh
fi

exit 0
