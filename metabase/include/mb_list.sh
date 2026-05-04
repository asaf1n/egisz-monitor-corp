# shellcheck shell=bash
# Нормализация ответов Metabase API к JSON-массиву (0.49+ часто { "data": [...] }, реже .items).
# Подключается из: provision.sh, setup-dashboards.sh, smoke-metabase-ui.sh
mb_list() {
  jq -c '
    if type == "array" then .
    elif (.data | type == "array") then .data
    elif (.items | type == "array") then .items
    elif (.data | type == "object") and (.data.items | type == "array") then .data.items
    else [] end
  '
}

# Metabase 0.48+: GET /api/dashboard без limit отдаёт только первую страницу — verify/smoke теряли дашборды
# за пределами offset 0 (счётчик «7 из 9», случайные FAIL после deploy). Собираем все страницы как в provision.sh.
# Args: base URL (no trailing slash), session token. stdout: JSON array of all dashboard objects.
mb_all_dashboards_json() {
  local _base="${1%/}"
  local _tok="$2"
  local _limit=200
  local _off=0
  local combined='[]'
  local _first_page_first_id=""
  while true; do
    local _page _arr _n _first_id
    _page="$(curl -sS "${_base}/api/dashboard?limit=${_limit}&offset=${_off}" \
      -H "X-Metabase-Session: ${_tok}" 2>/dev/null || echo '{}')"
    _arr="$(echo "${_page}" | mb_list)"
    _n="$(echo "${_arr}" | jq 'length')"
    if [ "${_n:-0}" -eq 0 ]; then
      break
    fi
    _first_id="$(echo "${_arr}" | jq -r '.[0].id // empty')"
    if [ "${_off}" -gt 0 ] && [ -n "${_first_page_first_id}" ] && [ "${_first_id}" = "${_first_page_first_id}" ]; then
      break
    fi
    if [ "${_off}" -eq 0 ]; then
      _first_page_first_id="${_first_id}"
    fi
    combined="$(jq -n --argjson a "${combined}" --argjson b "${_arr}" '$a + $b')"
    if [ "${_n}" -lt "${_limit}" ]; then
      break
    fi
    _off=$((_off + _limit))
    if [ "${_off}" -gt 20000 ]; then
      break
    fi
  done
  printf '%s\n' "${combined}"
}
