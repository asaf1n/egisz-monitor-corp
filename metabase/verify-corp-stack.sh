#!/bin/bash
# Проверка витрины Postgres + наличия коллекции/дашбордов EGISZ в Metabase (запуск из пода Metabase).
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
  jq -c 'if type == "array" then . elif (.data | type == "array") then .data else [] end'
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

log "Metabase: dashboards in admin personal collection (root)"
ME_JSON="$(curl -sS "${MB_URL}/api/user/current" -H "X-Metabase-Session: ${TOKEN}")"
ROOT_ID="$(echo "${ME_JSON}" | jq -r '.personal_collection_id // empty')"
if [ -z "${ROOT_ID}" ] || [ "${ROOT_ID}" = "null" ]; then
  log "FAIL: personal_collection_id missing (cannot verify dashboards placement)"
  exit 1
fi

ITEMS="$(curl -sS "${MB_URL}/api/collection/${ROOT_ID}/items" -H "X-Metabase-Session: ${TOKEN}")"
ND="$(echo "${ITEMS}" | mb_list | jq -r '[.[] | select(.model == "dashboard")] | length')"
ND="${ND:-0}"

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

log "Metabase OK (${ND} dashboard(s) in personal collection root, expected files=${EXPECTED})"
log "В UI: откройте «Персональная коллекция …» (collection_id=${ROOT_ID}) — дашборды на этой странице."
exit 0
