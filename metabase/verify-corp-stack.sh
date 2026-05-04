#!/bin/bash
# Раньше: гейтинг Postgres/Metabase в пайплайне deploy. Сейчас не вызывается из start.ps1 и provision.sh.
# Опционально вручную в поде Metabase, например: curl -sf http://localhost:3000/api/health
set -euo pipefail
echo "[verify-corp-stack] no-op (stack checks removed from deploy/apply; Metabase dashboards load in background)"
exit 0
