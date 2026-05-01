#!/bin/bash
# 7 запросов к Metabase API (облегчённый smoke): health, сессия, дашборды, 09+01, DWH.
# Тяжёлые шаги (HTML /, session/properties, POST card/query) убраны — они дублируют verify-corp-stack и сильно тормозят.
# Из verify вызывается только при VERIFY_FULL_UI_SMOKE=1; вручную: MB_URL=... ./smoke-metabase-ui.sh
set -euo pipefail

MB_URL="${MB_URL:-http://127.0.0.1:3000}"
ADMIN_EMAIL="${ADMIN_EMAIL:-admin@egisz.local}"
ADMIN_PASSWORD="${ADMIN_PASSWORD:-egisz}"
TOTAL_STEPS=7

step() {
  printf '[%s/%s] %s\n' "$1" "${TOTAL_STEPS}" "$2"
}

die() {
  echo "[smoke-ui] FAIL: $*" >&2
  exit 1
}

mb_json_list() {
  jq -c 'if type == "array" then . elif (.data | type == "array") then .data else [] end'
}

step 1 "GET /api/health"
code="$(curl -sS -o /dev/null -w '%{http_code}' "${MB_URL}/api/health")"
[[ "${code}" == "200" ]] || die "health HTTP ${code}"

step 2 "POST /api/session"
TOKEN="$(curl -sS -X POST "${MB_URL}/api/session" \
  -H "Content-Type: application/json" \
  -d "{\"username\":\"${ADMIN_EMAIL}\",\"password\":\"${ADMIN_PASSWORD}\"}" | jq -r '.id // empty')"
[[ -n "${TOKEN}" && "${TOKEN}" != "null" ]] || die "session token empty"

HDR=(-H "X-Metabase-Session: ${TOKEN}" -H "Content-Type: application/json")

step 3 "GET /api/user/current (personal_collection_id)"
ROOT_ID="$(curl -sS "${MB_URL}/api/user/current" "${HDR[@]}" | jq -r '.personal_collection_id // empty')"
[[ -n "${ROOT_ID}" && "${ROOT_ID}" != "null" ]] || die "personal_collection_id missing"

step 4 "GET /api/collection + /api/dashboard (дашборды в personal/namespaced коллекциях)"
# provision.sh кладёт дашборды в namespaced collection «ЕГИСЗ Мониторинг сервиса интеграции»
# с is_personal=true; ROOT_ID = личная коллекция админа (отдельная). Smoke считает дашборды в обеих.
PERSONAL_IDS_JSON="$(curl -sS "${MB_URL}/api/collection" "${HDR[@]}" \
  | mb_json_list \
  | jq -c --arg rid "${ROOT_ID}" '[.[] | select((.is_personal == true) or ((.id | tostring) == ($rid | tostring))) | (.id | tostring)] | unique')"
PERSONAL_IDS_JSON="${PERSONAL_IDS_JSON:-[]}"

DASHBOARDS="$(curl -sS "${MB_URL}/api/dashboard" "${HDR[@]}" | mb_json_list \
  | jq -c --argjson pids "${PERSONAL_IDS_JSON}" '
      [.[] | select(.collection_id != null and ((.collection_id | tostring) | IN($pids[])))]
    ')"
ND="$(echo "${DASHBOARDS}" | jq 'length')"
[[ "${ND:-0}" -ge 1 ]] || die "no dashboards in any personal collection"

EXEC_ID="$(echo "${DASHBOARDS}" | jq -r '.[] | select(.name=="09 Управленческий дашборд") | .id' | head -n1)"
OP_ID="$(echo "${DASHBOARDS}" | jq -r '.[] | select(.name=="01 Оперативный мониторинг") | .id' | head -n1)"
[[ -n "${EXEC_ID}" && "${EXEC_ID}" != "null" ]] || die "dashboard 09 not found"
[[ -n "${OP_ID}" && "${OP_ID}" != "null" ]] || die "dashboard 01 not found"

step 5 "GET /api/dashboard/:id — Управленческий (parameters, auto_apply, mappings)"
DEX="$(curl -sS "${MB_URL}/api/dashboard/${EXEC_ID}" "${HDR[@]}")"
echo "${DEX}" | jq -e '.auto_apply_filters == true' >/dev/null || die "09: auto_apply_filters is not true"
NP="$(echo "${DEX}" | jq '.parameters | length')"
[[ "${NP}" -ge 1 ]] || die "09: expected dashboard parameters, got ${NP}"
DC09="$(echo "${DEX}" | jq '.dashcards // .ordered_cards // []')"
MAPS="$(echo "${DC09}" | jq '[.[] | select(.card != null or .card_id != null) | (.parameter_mappings // []) | length] | add // 0')"
[[ "${MAPS}" -gt 0 ]] || die "09: no parameter_mappings on any dashcard (filters will not work)"

step 6 "GET /api/dashboard/:id — Оперативный (filters wired)"
DOP="$(curl -sS "${MB_URL}/api/dashboard/${OP_ID}" "${HDR[@]}")"
echo "${DOP}" | jq -e '.auto_apply_filters == true' >/dev/null || die "01: auto_apply_filters is not true"
echo "${DOP}" | jq -e '(.parameters // []) | map(.slug) | index("top_semd_filter") != null and index("top_clinic_filter") != null' >/dev/null \
  || die "01: expected dashboard parameter slugs top_semd_filter and top_clinic_filter (URL/bookmarks)"
DC01="$(echo "${DOP}" | jq '.dashcards // .ordered_cards // []')"
MAPS2="$(echo "${DC01}" | jq '[.[] | select(.card != null or .card_id != null) | (.parameter_mappings // []) | length] | add // 0')"
[[ "${MAPS2}" -gt 0 ]] || die "01: no parameter_mappings on dashcards"

step 7 "GET /api/database (витрина EGISZ Corp DWH)"
DBS="$(curl -sS "${MB_URL}/api/database" "${HDR[@]}")"
echo "${DBS}" | mb_json_list | jq -e '.[] | select(.name == "EGISZ Corp DWH" or .name == "egisz_reports")' >/dev/null || die "DWH database not registered"

echo "[smoke-ui] OK — ${TOTAL_STEPS} шагов: health, сессия, дашборды 09/01 (filters), DWH (без HTML/card query)."
