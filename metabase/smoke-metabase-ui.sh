#!/bin/bash
# ~10 запросов к Metabase API: здоровье, сессия, коллекция, два ключевых дашборда (фильтры + auto_apply),
# главная страница UI, БД DWH. Запуск из пода Metabase (MB_URL=http://localhost:3000) или с хоста после port-forward.
set -euo pipefail

MB_URL="${MB_URL:-http://127.0.0.1:3000}"
ADMIN_EMAIL="${ADMIN_EMAIL:-admin@egisz.local}"
ADMIN_PASSWORD="${ADMIN_PASSWORD:-egisz}"

step() {
  printf '[%s/10] %s\n' "$1" "$2"
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

step 4 "GET /api/collection/:id/items (дашборды в корне)"
ITEMS="$(curl -sS "${MB_URL}/api/collection/${ROOT_ID}/items" "${HDR[@]}")"
ND="$(echo "${ITEMS}" | mb_json_list | jq '[.[] | select(.model == "dashboard")] | length')"
[[ "${ND:-0}" -ge 1 ]] || die "no dashboards in personal collection root"

EXEC_ID="$(echo "${ITEMS}" | mb_json_list | jq -r '.[] | select(.model=="dashboard" and .name=="09 Управленческий дашборд") | .id' | head -n1)"
OP_ID="$(echo "${ITEMS}" | mb_json_list | jq -r '.[] | select(.model=="dashboard" and .name=="01 Оперативный мониторинг") | .id' | head -n1)"
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
DC01="$(echo "${DOP}" | jq '.dashcards // .ordered_cards // []')"
MAPS2="$(echo "${DC01}" | jq '[.[] | select(.card != null or .card_id != null) | (.parameter_mappings // []) | length] | add // 0')"
[[ "${MAPS2}" -gt 0 ]] || die "01: no parameter_mappings on dashcards"

step 7 "GET /api/database (витрина EGISZ Corp DWH)"
DBS="$(curl -sS "${MB_URL}/api/database" "${HDR[@]}")"
echo "${DBS}" | mb_json_list | jq -e '.[] | select(.name == "EGISZ Corp DWH" or .name == "egisz_reports")' >/dev/null || die "DWH database not registered"

step 8 "GET / (HTML главная Metabase — как открытие в браузере)"
code="$(curl -sS -o /dev/null -w '%{http_code}' "${MB_URL}/")"
[[ "${code}" =~ ^(200|302)$ ]] || die "main page HTTP ${code}"

step 9 "GET /api/session/properties (инициализация UI)"
PROP_CODE="$(curl -sS -o /tmp/smoke_props.json -w '%{http_code}' "${MB_URL}/api/session/properties")"
[[ "${PROP_CODE}" =~ ^2 ]] || die "session/properties HTTP ${PROP_CODE}"
jq -e '."has-user-setup"' /tmp/smoke_props.json >/dev/null || die "unexpected session/properties JSON"

step 10 "POST /api/card/:id/query — выполнение SQL карточки (результат как в UI)"
CARD_ID="$(echo "${DEX}" | jq -r '(.dashcards // .ordered_cards // [])[0].card_id // empty')"
[[ -n "${CARD_ID}" && "${CARD_ID}" != "null" ]] || die "no card_id on first dashcard Управленческого дашборда"
# Только параметры со slug «period»: у карточек 09 шаблон {{period}}, остальные id дают Metabase «нет шаблонного тега … nil».
QBODY="$(echo "${DEX}" | jq -c --argjson did "${EXEC_ID}" '{ignore_cache: false, dashboard_id: $did, parameters: [.parameters[]? | select((.slug // "") == "period") | {id: .id, value: (.default // 30)}]}')"
HTTP_Q="$(curl -sS -o /tmp/smoke_card_query.json -w '%{http_code}' -X POST "${MB_URL}/api/card/${CARD_ID}/query" \
  "${HDR[@]}" -d "${QBODY}")"
[[ "${HTTP_Q}" =~ ^2 ]] || { cat /tmp/smoke_card_query.json >&2 || true; die "card query HTTP ${HTTP_Q}"; }
if jq -e '.status == "failed"' /tmp/smoke_card_query.json >/dev/null 2>&1; then
  cat /tmp/smoke_card_query.json >&2
  die "card query status failed"
fi

echo "[smoke-ui] OK — 10 шагов, фильтры привязаны (parameter_mappings>0), auto_apply включён, карточка выполнила запрос."
