#!/bin/bash
set -euo pipefail

METABASE_URL="${METABASE_URL:-http://localhost:3000}"
ADMIN_EMAIL="${ADMIN_EMAIL:-admin@egisz-monitor.local}"
ADMIN_PASSWORD="${ADMIN_PASSWORD:-ChangeMeNow123!}"
DB_NAME="${DB_NAME:-egisz_corp}"
DB_USER="${DB_USER:-egisz_corp}"
DB_PASSWORD="${DB_PASSWORD:-egisz_corp}"
DB_DISPLAY_NAME="${DB_DISPLAY_NAME:-EGISZ Corp DWH}"
PGHOST="${PGHOST:-postgres}"
PGPORT="${PGPORT:-5432}"

ROOT_COLLECTION_NAME="EGISZ Corp Monitoring"

log_info() {
  echo "[dashboards] $1" >&2
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

  child_ids="$(echo "${children_json}" | jq -r '.data[]? | select(.model == "collection") | .id')"
  for child_id in ${child_ids}; do
    delete_collection_tree "${child_id}"
  done

  dashboard_ids="$(echo "${children_json}" | jq -r '.data[]? | select(.model == "dashboard") | .id')"
  for dashboard_id in ${dashboard_ids}; do
    api_request DELETE "/api/dashboard/${dashboard_id}" >/dev/null
  done

  card_ids="$(echo "${children_json}" | jq -r '.data[]? | select(.model == "card") | .id')"
  for card_id in ${card_ids}; do
    api_request DELETE "/api/card/${card_id}" >/dev/null
  done

  api_request PUT "/api/collection/${collection_id}" '{"archived":true}' >/dev/null
  api_request DELETE "/api/collection/${collection_id}" >/dev/null
}

delete_demo_content() {
  local collections_json dashboards_json databases_json example_ids dashboard_ids sample_ids

  collections_json="$(api_request GET "/api/collection")"
  example_ids="$(echo "${collections_json}" | jq -r '.[] | select(.name == "Examples") | .id')"
  for example_id in ${example_ids}; do
    delete_collection_tree "${example_id}"
  done

  dashboards_json="$(api_request GET "/api/dashboard")"
  dashboard_ids="$(echo "${dashboards_json}" | jq -r '.[] | select(.name == "E-commerce Insights") | .id')"
  for dashboard_id in ${dashboard_ids}; do
    api_request DELETE "/api/dashboard/${dashboard_id}" >/dev/null
  done

  databases_json="$(api_request GET "/api/database")"
  sample_ids="$(echo "${databases_json}" | jq -r '.data[] | select(.is_sample == true or .name == "Sample Database") | .id')"
  for sample_id in ${sample_ids}; do
    api_request DELETE "/api/database/${sample_id}" >/dev/null
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
  local template_tags="$(echo "$parsed_json" | jq -c '.dataset_query.native["template-tags"] // {}')"
  local table_ref="$(echo "$parsed_json" | jq -r '.table_ref // empty')"
  local table_id=""
  local visualization_settings="$(echo "$parsed_json" | jq -c '
    (.visualization_settings // {}) as $vs
    | $vs
    | .table = (.table // {})
    | .table.columns = ((.table.columns // {}) + {
        "[\"name\",\"clinic_id\"]": { "display_as": null },
        "[\"name\",\"service_id\"]": { "display_as": null },
        "[\"name\",\"transaction_id\"]": { "display_as": null }
      })
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
  
  # First, create cards and build dashcards array
  local dashcards="[]"
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
      mappings="$(echo "$parsed_json" | jq -c --argjson cardIndex "$i" '
        (.cards[$cardIndex].dataset_query.native["template-tags"] // {} | keys) as $cardTags
        | [
            (.parameters // [])[] as $param
            | (
                if ($param.slug | endswith("_filter")) then
                  ($param.slug | sub("_filter$"; ""))
                else
                  $param.slug
                end
              ) as $tagName
            | if (($cardTags | index($tagName)) != null) then
                { parameter_id: $param.id, target: ["variable", ["template-tag", $tagName]] }
              else
                empty
              end
          ]
      ')"
      
      local dashcard="$(jq -n \
        --arg cardId "$card_id" \
        --arg sizeX "$sizeX" \
        --arg sizeY "$sizeY" \
        --arg row "$row" \
        --arg col "$col" \
        --arg mappings "$mappings" \
        '{
          card_id: ($cardId | tonumber),
          size_x: ($sizeX | tonumber),
          size_y: ($sizeY | tonumber),
          row: ($row | tonumber),
          col: ($col | tonumber),
          parameter_mappings: ($mappings | fromjson)
        }')"
        
      dashcards="$(echo "$dashcards" | jq --arg dc "$dashcard" '. + [($dc | fromjson)]')"
    done
  fi

  local payload
  payload="$(jq -n \
    --arg name "${name}" \
    --arg description "${description}" \
    --arg collectionId "${collection_id}" \
    --arg dashcards "${dashcards}" \
    --arg parameters "${parameters_json}" \
    '{
      name: $name,
      description: $description,
      collection_id: ($collectionId | tonumber),
      cacheables: [],
      dashcards: ($dashcards | fromjson),
      parameters: ($parameters | fromjson)
    }')"

  api_request POST "/api/dashboard" "${payload}" | jq -r '.id'
}

log_info "Waiting for Metabase at ${METABASE_URL}..."
until curl --silent --fail "${METABASE_URL}/api/health" >/dev/null; do
  sleep 3
done

authenticate
delete_demo_content

for collection_id in $(api_request GET "/api/collection" | jq -r '.[] | select(.name | test("^EGISZ")) | .id' | sort -nr); do
  delete_collection_tree "${collection_id}"
done

authenticate
APP_DB_ID="$(ensure_app_database)"

ROOT_COLLECTION_ID="$(create_collection "${ROOT_COLLECTION_NAME}" "EGISZ dashboards collection" "#509EE3")"

for dashboard_file in /app/metabase_dashboards/*.json; do
  if [ -f "$dashboard_file" ]; then
    log_info "Provisioning $dashboard_file..."
    create_dashboard "$dashboard_file" "$ROOT_COLLECTION_ID"
  fi
done

log_info "Database: ${APP_DB_ID}"
log_info "Root collection: ${ROOT_COLLECTION_ID}"
