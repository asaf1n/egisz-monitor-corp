#!/bin/bash
set -euo pipefail
echo "Starting Metabase..."
/app/run_metabase.sh &
METABASE_PID=$!
trap 'kill "${METABASE_PID}" 2>/dev/null || true' INT TERM
( /app/provision.sh || echo "[entrypoint] provision failed" ) &
wait "${METABASE_PID}"
