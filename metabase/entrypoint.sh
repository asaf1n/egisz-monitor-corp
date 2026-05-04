#!/bin/bash
set -euo pipefail
# Точка входа образа Metabase: поднять JVM Metabase, параллельно /app/provision.sh (первичная настройка API,
# условный вызов setup-dashboards.sh, public link). См. metabase/Dockerfile.
echo "Starting Metabase..."
/app/run_metabase.sh &
METABASE_PID=$!
trap 'kill "${METABASE_PID}" 2>/dev/null || true' INT TERM
( /app/provision.sh || echo "[entrypoint] provision failed" ) &
wait "${METABASE_PID}"
