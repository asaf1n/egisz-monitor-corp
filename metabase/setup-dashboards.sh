#!/bin/bash
# Единственный путь поставки отчётов EGISZ в Metabase: все дашборды и native-карточки из
# DASHBOARDS_DIR/*.json (см. metabase_dashboards/README.md). Пустой инстанс после первой настройки
# админа + готовой схемы Postgres — этот скрипт создаёт весь набор. Повторы: удаление одноимённых
# сущностей в персональной коллекции, затем повторный импорт.
set -euo pipefail

METABASE_URL="${METABASE_URL:-http://localhost:3000}"
# Fallback, если переменные не заданы (в поде k8s задаются из Secret metabase-admin).
ADMIN_EMAIL="${ADMIN_EMAIL:-admin@egisz.local}"
ADMIN_PASSWORD="${ADMIN_PASSWORD:-egisz}"
DB_NAME="${DB_NAME:-egisz_reports}"
DB_USER="${DB_USER:-egisz}"
DB_PASSWORD="${DB_PASSWORD:-egisz}"
DB_DISPLAY_NAME="${DB_DISPLAY_NAME:-EGISZ Corp DWH}"
PGHOST="${PGHOST:-postgres}"
PGPORT="${PGPORT:-5432}"
# Каталог с JSON дашбордов (в образе /app/metabase_dashboards; локально можно смонтировать репозиторий).
DASHBOARDS_DIR="${METABASE_DASHBOARDS_DIR:-/app/metabase_dashboards}"

ROOT_COLLECTION_NAME="EGISZ Corp Monitoring"

log_info() {
  echo "[dashboards] $1" >&2
}

# Metabase 0.49+ часто отдаёт списки как { "data": [ ... ] }; без нормализации jq '.[]' ломается и провижининг молча пропускается.
mb_normalize_list() {
  jq -c 'if type == "array" then . elif (.data | type == "array") then .data else [] end'
}

api_request() {
  local method="$1"
  local path="$2"
  local payload="${3:-}"
  local response

  if [ -n "${payload}" ]; then
    response="$(curl -sS -w $'\n%{http_code}' -X "${method}" "${METABASE_URL}${path}" \
      -H "Content-Type: application/json" \
      -H "X-Metabase-Session: ${SESSION_TOKEN}" \
      -d "${payload}")"
  else
    response="$(curl -sS -w $'\n%{http_code}' -X "${method}" "${METABASE_URL}${path}" \
      -H "X-Metabase-Session: ${SESSION_TOKEN}")"
  fi

  HTTP_CODE="$(echo "${response}" | tail -n1)"
  RESPONSE_BODY="$(echo "${response}" | sed '$d')"

  if [[ ! "${HTTP_CODE}" =~ ^2 ]]; then
    echo "Metabase API ${method} ${path} failed with HTTP ${HTTP_CODE}" >&2
    echo "${RESPONSE_BODY}" >&2
    return 1
  else
    if [[ "${method}" == "POST" && ( "${path}" == "/api/card" || "${path}" == "/api/dashboard" ) ]]; then
      echo "Metabase API ${method} ${path} successful with HTTP ${HTTP_CODE} OK" >&2
    fi
    if [[ "${method}" == "PUT" && "${path}" =~ ^/api/dashboard/[0-9]+/cards$ ]]; then
      echo "Metabase API ${method} ${path} successful with HTTP ${HTTP_CODE} OK" >&2
    fi
  fi

  printf '%s' "${RESPONSE_BODY}"
}

authenticate() {
  SESSION_TOKEN="$(curl -sS -X POST "${METABASE_URL}/api/session" \
    -H "Content-Type: application/json" \
    -d "{\"username\":\"${ADMIN_EMAIL}\",\"password\":\"${ADMIN_PASSWORD}\"}" | jq -r '.id')"

  if [ -z "${SESSION_TOKEN}" ] || [ "${SESSION_TOKEN}" = "null" ]; then
    echo "Failed to authenticate in Metabase" >&2
    exit 1
  fi
}

delete_collection_tree() {
  local collection_id="$1"
  local children_json child_ids dashboard_ids card_ids

  children_json="$(api_request GET "/api/collection/${collection_id}/items")"

  child_ids="$(echo "${children_json}" | mb_normalize_list | jq -r '.[]? | select(.model == "collection") | .id')"
  for child_id in ${child_ids}; do
    delete_collection_tree "${child_id}"
  done

  dashboard_ids="$(echo "${children_json}" | mb_normalize_list | jq -r '.[]? | select(.model == "dashboard") | .id')"
  for dashboard_id in ${dashboard_ids}; do
    api_request DELETE "/api/dashboard/${dashboard_id}" >/dev/null
  done

  card_ids="$(echo "${children_json}" | mb_normalize_list | jq -r '.[]? | select(.model == "card") | .id')"
  for card_id in ${card_ids}; do
    api_request DELETE "/api/card/${card_id}" >/dev/null
  done

  api_request PUT "/api/collection/${collection_id}" '{"archived":true}' >/dev/null
  api_request DELETE "/api/collection/${collection_id}" >/dev/null
}

delete_demo_content() {
  local collections_raw collections_json dashboards_raw dashboards_json databases_json example_ids dashboard_ids sample_ids

  collections_raw="$(api_request GET "/api/collection")"
  collections_json="$(echo "${collections_raw}" | mb_normalize_list)"
  example_ids="$(echo "${collections_json}" | jq -r '.[] | select(.name == "Examples") | .id')"
  for example_id in ${example_ids}; do
    delete_collection_tree "${example_id}"
  done

  dashboards_raw="$(api_request GET "/api/dashboard")"
  dashboards_json="$(echo "${dashboards_raw}" | mb_normalize_list)"
  dashboard_ids="$(echo "${dashboards_json}" | jq -r '.[] | select(.name == "E-commerce Insights") | .id')"
  for dashboard_id in ${dashboard_ids}; do
    api_request DELETE "/api/dashboard/${dashboard_id}" >/dev/null
  done

  databases_json="$(api_request GET "/api/database")"
  sample_ids="$(echo "${databases_json}" | jq -r '.data[]? | select(.is_sample == true or .name == "Sample Database") | .id')"
  for sample_id in ${sample_ids}; do
    api_request DELETE "/api/database/${sample_id}" >/dev/null
  done
}

# Сохранённые вопросы вне коллекции EGISZ (старые SQL к snake_case-витрине) не обновляются при деплое.
delete_legacy_corp_cards() {
  local cards_raw ids
  if ! cards_raw="$(api_request GET "/api/card")"; then
    log_info "GET /api/card failed; skip legacy card cleanup"
    return 0
  fi
  ids="$(echo "${cards_raw}" | mb_normalize_list | jq -r '
    .[]
    | select(
        (.name == "Факты со статусом error")
        or (
          ((.dataset_query.native.query // "") | test("v_egisz_transactions_enriched"))
          and ((.dataset_query.native.query // "") | (test("v_egisz_transactions_enriched_ui") | not))
        )
      )
    | .id
  ')"
  for card_id in ${ids}; do
    [ -z "${card_id}" ] || [ "${card_id}" = "null" ] && continue
    log_info "Deleting legacy saved question card id=${card_id}"
    api_request DELETE "/api/card/${card_id}" >/dev/null || true
  done
}

ensure_app_database() {
  local databases_json db_id payload

  databases_json="$(api_request GET "/api/database")"
  db_id="$(echo "${databases_json}" | jq -r --arg dbName "${DB_NAME}" --arg display "${DB_DISPLAY_NAME}" '
    [
      .data[]
      | select(
          (.name == $display)
          or (.name == $dbName)
          or (.details.dbname? == $dbName)
        )
    ]
    | sort_by(.id)
    | last
    | .id // empty
  ')"

  if [ -n "${db_id}" ]; then
    printf '%s' "${db_id}"
    return 0
  fi

  payload="$(jq -n \
    --arg name "${DB_DISPLAY_NAME}" \
    --arg dbname "${DB_NAME}" \
    --arg user "${DB_USER}" \
    --arg password "${DB_PASSWORD}" \
    --arg pgHost "${PGHOST}" \
    --arg pgPort "${PGPORT}" \
    '{
      engine: "postgres",
      name: $name,
      details: {
        host: $pgHost,
        port: ($pgPort | tonumber),
        dbname: $dbname,
        user: $user,
        password: $password,
        ssl: false,
        "tunnel-enabled": false,
        "advanced-options": false
      },
      is_full_sync: true,
      is_on_demand: false,
      auto_run_queries: true
    }')"

  db_id="$(api_request POST "/api/database" "${payload}" | jq -r '.id // empty')"

  if [ -z "${db_id}" ] || [ "${db_id}" = "null" ]; then
    echo "Failed to register application database" >&2
    exit 1
  fi

  api_request POST "/api/database/${db_id}/sync_schema" "{}" >/dev/null || true
  printf '%s' "${db_id}"
}

get_database_metadata() {
  if [ -z "${APP_DB_METADATA_JSON:-}" ]; then
    APP_DB_METADATA_JSON="$(api_request GET "/api/database/${APP_DB_ID}/metadata?include_hidden=true")"
  fi

  printf '%s' "${APP_DB_METADATA_JSON}"
}

resolve_table_id() {
  local table_ref="$1"

  if [ -z "${table_ref}" ] || [ "${table_ref}" = "null" ]; then
    return 0
  fi

  local table_id
  table_id="$(
    get_database_metadata | jq -r --arg tableRef "${table_ref}" '
      [
        .tables[]?
        | select(
            .name == $tableRef
            or ((.schema // "public") + "." + .name) == $tableRef
          )
      ]
      | sort_by(.id)
      | last
      | .id // empty
    '
  )"

  if [ -z "${table_id}" ] || [ "${table_id}" = "null" ]; then
    echo "Failed to resolve Metabase table ID for ${table_ref}" >&2
    exit 1
  fi

  printf '%s' "${table_id}"
}

create_collection() {
  local name="$1"
  local description="$2"
  local color="$3"
  local parent_id="${4:-}"
  local payload

  if [ -n "${parent_id}" ]; then
    payload="$(jq -n \
      --arg name "${name}" \
      --arg description "${description}" \
      --arg color "${color}" \
      --arg parentId "${parent_id}" \
      '{name: $name, description: $description, color: $color, parent_id: ($parentId | tonumber)}') "
  else
    payload="$(jq -n \
      --arg name "${name}" \
      --arg description "${description}" \
      --arg color "${color}" \
      '{name: $name, description: $description, color: $color}')"
  fi

  api_request POST "/api/collection" "${payload}" | jq -r '.id'
}

create_card() {
  local file_json="$1"
  local collection_id="$2"
  
  if ! parsed_json="$(cat "$file_json" | jq -r .)"; then
    echo "Failed to parse $file_json. Exiting." >&2
    exit 1
  fi
  
  local name="$(echo "$parsed_json" | jq -r '.name')"
  local description="$(echo "$parsed_json" | jq -r '.description')"
  local query="$(echo "$parsed_json" | jq -r '.dataset_query.native.query')"
  local display="$(echo "$parsed_json" | jq -r '.display')"
  local meta_json
  meta_json="$(get_database_metadata)"
  local template_tags
  template_tags="$(
    echo "$parsed_json" | jq -c --argjson meta "$meta_json" '
      def resolve_field_id($meta; $tr; $fn):
        [
          $meta.tables[]?
          | select(.name == $tr or ((.schema // "public") + "." + .name) == $tr)
          | .fields[]?
          | select(.name == $fn or .display_name == $fn)
          | .id
        ] | first;
      (.dataset_query.native["template-tags"] // {}) as $tags
      | (.["metabase-field-filters"] // {}) as $ff
      | if ($ff | length) == 0 then
          $tags
        else
          ($ff | keys_unsorted) as $keys
          | reduce $keys[] as $k ($tags;
              resolve_field_id($meta; $ff[$k].table_ref; $ff[$k].field_name) as $fid
              | if $fid == null then
                  error("metabase-field-filters: field not found: \($ff[$k].table_ref).\($ff[$k].field_name)")
                else
                  .[$k] = ($tags[$k] // {}) + { dimension: ["field", $fid, null] }
                end
            )
        end
    '
  )"
  local table_ref="$(echo "$parsed_json" | jq -r '.table_ref // empty')"
  local table_id=""
  local visualization_settings="$(echo "$parsed_json" | jq -c \
    --arg display "$display" \
    --arg query "$query" '
    (.visualization_settings // {}) as $vs
    | $vs
    | .table = (.table // {})
    | .table.columns = (
        if ($display == "table")
           and ($query | test("v_egisz_transactions_enriched_ui|stg_parse_errors")) then
          [ { "name": "Связанное сообщение", "enabled": false } ]
        else
          ((.table.columns // {}) + {
            "[\"name\",\"clinic_id\"]": { "display_as": null },
            "[\"name\",\"service_id\"]": { "display_as": null },
            "[\"name\",\"transaction_id\"]": { "display_as": null }
          })
        end
      )
  ')"

  if [ -n "${table_ref}" ]; then
    table_id="$(resolve_table_id "${table_ref}")"
  fi

  local payload
  payload="$(jq -n \
    --arg name "${name}" \
    --arg description "${description}" \
    --arg query "${query}" \
    --arg display "${display}" \
    --arg templateTags "${template_tags}" \
    --arg visualizationSettings "${visualization_settings}" \
    --arg collectionId "${collection_id}" \
    --arg databaseId "${APP_DB_ID}" \
    --arg tableId "${table_id}" \
    '{
      name: $name,
      description: $description,
      collection_id: ($collectionId | tonumber),
      dataset_query: {
        type: "native",
        native: {
          query: $query,
          "template-tags": ($templateTags | fromjson)
        },
        database: ($databaseId | tonumber)
      },
      table_id: (if ($tableId | length) > 0 then ($tableId | tonumber) else null end),
      display: $display,
      visualization_settings: ($visualizationSettings | fromjson)
    }')"

  api_request POST "/api/card" "${payload}" | jq -r '.id'
}

create_dashboard() {
  local file_json="$1"
  local collection_id="$2"
  
  if ! parsed_json="$(cat "$file_json" | jq -r .)"; then
    echo "Failed to parse $file_json. Exiting." >&2
    exit 1
  fi
  
  local name="$(echo "$parsed_json" | jq -r '.name')"
  local description="$(echo "$parsed_json" | jq -r '.description')"
  local parameters_json="$(echo "$parsed_json" | jq -c '.parameters // []')"
  local dash_width="$(echo "$parsed_json" | jq -r '.width // "full"')"

  local dashboard_payload dashboard_id
  dashboard_payload="$(jq -n \
    --arg name "${name}" \
    --arg description "${description}" \
    --arg collectionId "${collection_id}" \
    --arg parameters "${parameters_json}" \
    --arg width "${dash_width}" \
    '{
      name: $name,
      description: $description,
      collection_id: ($collectionId | tonumber),
      width: $width,
      cacheables: [],
      parameters: ($parameters | fromjson),
      auto_apply_filters: true
    }')"

  dashboard_id="$(api_request POST "/api/dashboard" "${dashboard_payload}" | jq -r '.id')"
  if [ -z "${dashboard_id}" ] || [ "${dashboard_id}" = "null" ]; then
    echo "Failed to create dashboard ${name}" >&2
    exit 1
  fi

  # Id параметров фильтра после POST могут отличаться от полей в JSON — привязки к карточкам строим по ответу GET.
  local dash_saved resolved_parameters_json
  dash_saved="$(api_request GET "/api/dashboard/${dashboard_id}")"
  resolved_parameters_json="$(echo "${dash_saved}" | jq -c '.parameters // []')"

  # Metabase v0.47+ attaches dashboard cards via PUT /api/dashboard/:id/cards.
  local cards="[]"
  local num_cards="$(echo "$parsed_json" | jq '.cards | length')"
  if [ "$num_cards" -gt 0 ]; then
    for i in $(seq 0 $((num_cards - 1))); do
      local card_file="/tmp/card_${i}.json"
      echo "$parsed_json" | jq -c ".cards[$i]" > "$card_file"
      local card_id="$(create_card "$card_file" "$collection_id")"
      
      local sizeX="$(echo "$parsed_json" | jq -r ".cards[$i].sizeX // 4")"
      local sizeY="$(echo "$parsed_json" | jq -r ".cards[$i].sizeY // 4")"
      local row="$(echo "$parsed_json" | jq -r ".cards[$i].row // 0")"
      local col="$(echo "$parsed_json" | jq -r ".cards[$i].col // 0")"
      
      local mappings
      mappings="$(echo "$parsed_json" | jq -c --argjson cardIndex "$i" --argjson dashParams "${resolved_parameters_json}" '
        (.cards[$cardIndex].dataset_query.native["template-tags"] // {}) as $cardTemplateTags
        | ($cardTemplateTags | keys) as $cardTags
        | [
            $dashParams[] as $param
            | (
                if ($param.slug | endswith("_filter")) then
                  ($param.slug | sub("_filter$"; ""))
                else
                  $param.slug
                end
              ) as $tagName
            | if (($cardTags | index($tagName)) != null) then
                ($cardTemplateTags[$tagName].type // "") as $ttype
                | if $ttype == "dimension" then
                    { parameter_id: $param.id, target: ["dimension", ["template-tag", $tagName]] }
                  else
                    { parameter_id: $param.id, target: ["variable", ["template-tag", $tagName]] }
                  end
              else
                empty
              end
          ]
      ')"

      # Negative IDs mean "new dashboard card" for the bulk update endpoint.
      local dashcard_id=$((-(i + 1)))
      local dashcard="$(jq -n \
        --arg dashcardId "$dashcard_id" \
        --arg cardId "$card_id" \
        --arg sizeX "$sizeX" \
        --arg sizeY "$sizeY" \
        --arg row "$row" \
        --arg col "$col" \
        --arg mappings "$mappings" \
        '{
          id: ($dashcardId | tonumber),
          card_id: ($cardId | tonumber),
          size_x: ($sizeX | tonumber),
          size_y: ($sizeY | tonumber),
          row: ($row | tonumber),
          col: ($col | tonumber),
          parameter_mappings: ($mappings | fromjson),
          series: [],
          visualization_settings: {}
        }')"

      cards="$(echo "$cards" | jq --arg dc "$dashcard" '. + [($dc | fromjson)]')"
    done
  fi

  if [ "$num_cards" -gt 0 ]; then
    local cards_payload
    cards_payload="$(jq -n --arg cards "${cards}" '{cards: ($cards | fromjson)}')"
    api_request PUT "/api/dashboard/${dashboard_id}/cards" "${cards_payload}" >/dev/null

    # Полное тело как после GET (уже с parameter_mappings), с auto_apply_filters — обходит сброс при PUT …/cards в MB 0.48+.
    local dash_after fix_payload
    dash_after="$(api_request GET "/api/dashboard/${dashboard_id}")"
    fix_payload="$(echo "${dash_after}" | jq --arg w "${dash_width}" '.auto_apply_filters = true | .width = $w')"
    if ! api_request PUT "/api/dashboard/${dashboard_id}" "${fix_payload}" >/dev/null; then
      log_info "WARN: PUT /api/dashboard/${dashboard_id} after dashcards failed (filters may need Apply in UI)"
    fi
  fi

  printf '%s' "${dashboard_id}"
}

log_info "Waiting for Metabase at ${METABASE_URL}..."
until curl --silent --fail "${METABASE_URL}/api/health" >/dev/null; do
  sleep 3
done

authenticate
delete_demo_content

collections_for_delete="$(api_request GET "/api/collection" | mb_normalize_list)"
for collection_id in $(echo "${collections_for_delete}" | jq -r '.[] | select(.name | test("^EGISZ")) | .id' | sort -nr); do
  delete_collection_tree "${collection_id}"
done

authenticate
delete_legacy_corp_cards

authenticate
APP_DB_ID="$(ensure_app_database)"

# Metabase 0.60+ sync может отставать: без полей витрины create_card (field filters) падает с «metabase-field-filters: field not found».
log_info "Waiting for DWH view field metadata (field filters on «Обработано»)…"
for _i in $(seq 1 90); do
  APP_DB_METADATA_JSON="" # сбрасываем кэш get_database_metadata; иначе застреваем на первом (неполном) снимке
  _nf="$(get_database_metadata | jq '
    [ .tables[]? | select(.name == "v_egisz_transactions_enriched_ui")
        | .fields[]?
        | select(
            .name == "processed_at" or .name == "Обработано" or .display_name == "Обработано"
          ) ]
    | length
  ')"
  if [ "${_nf:-0}" -ge 1 ] 2>/dev/null; then
    log_info "DWH view fields in metadata (count=${_nf}) — OK"
    break
  fi
  if [ "$_i" -eq 90 ]; then
    echo "[dashboards] ERROR: v_egisz_transactions_enriched_ui (Обработано) not in metadata after 180s — field filters will fail" >&2
    exit 1
  fi
  sleep 2
done
APP_DB_METADATA_JSON=""

# Дашборды в корне личной коллекции (тот же URL, что «Персональная коллекция …»), иначе вложенная папка не видна на главном экране коллекции.
PERSONAL_ID="$(api_request GET "/api/user/current" | jq -r '.personal_collection_id // empty')"
if [ -n "${PERSONAL_ID}" ] && [ "${PERSONAL_ID}" != "null" ]; then
  log_info "Provisioning into admin personal_collection_id=${PERSONAL_ID} (root of personal collection in UI)"
  ROOT_COLLECTION_ID="${PERSONAL_ID}"
else
  log_info "WARN: personal_collection_id missing from /api/user/current; creating ${ROOT_COLLECTION_NAME} at default root"
  ROOT_COLLECTION_ID="$(create_collection "${ROOT_COLLECTION_NAME}" "EGISZ dashboards collection" "#509EE3")"
fi

# Очистка личной коллекции от всех дашбордов и карточек: при смене «имя в JSON» выборочное
# удаление по старым названиям оставляло дубликаты; тут полный сброс перед импортом.
wipe_corp_root_collection() {
  local coll="${1:-}"
  if [ -z "${coll}" ] || [ "${coll}" = "null" ]; then
    return 0
  fi
  local _pass items list id
  for _pass in 1 2 3; do
    items="$(api_request GET "/api/collection/${coll}/items")"
    list="$(echo "${items}" | mb_normalize_list)"
    # Вложенные коллекции (старые папки EGISZ и т.п.): иначе остаются «осиротевшие» карточки и дубликаты в UI.
    while IFS= read -r subcoll; do
      [ -z "${subcoll}" ] && continue
      log_info "Removing nested collection id=${subcoll} (full tree)"
      delete_collection_tree "${subcoll}"
    done < <(echo "${list}" | jq -r '.[] | select(.model == "collection") | .id')
    while IFS= read -r id; do
      [ -z "${id}" ] && continue
      log_info "Removing prior dashboard id=${id}"
      api_request DELETE "/api/dashboard/${id}" >/dev/null || true
    done < <(echo "${list}" | jq -r '.[] | select(.model == "dashboard") | .id')
    items="$(api_request GET "/api/collection/${coll}/items")"
    list="$(echo "${items}" | mb_normalize_list)"
    while IFS= read -r id; do
      [ -z "${id}" ] && continue
      log_info "Removing prior saved question (card) id=${id}"
      api_request DELETE "/api/card/${id}" >/dev/null || true
    done < <(echo "${list}" | jq -r '.[] | select(.model == "card") | .id')
  done
  log_info "Root collection ${coll} cleared; importing from ${DASHBOARDS_DIR}"
}

wipe_corp_root_collection "${ROOT_COLLECTION_ID}"

for dashboard_file in "${DASHBOARDS_DIR}"/*.json; do
  if [ -f "$dashboard_file" ]; then
    log_info "Provisioning $dashboard_file..."
    create_dashboard "$dashboard_file" "$ROOT_COLLECTION_ID"
  fi
done

log_info "Database: ${APP_DB_ID}"
log_info "Root collection: ${ROOT_COLLECTION_ID}"
