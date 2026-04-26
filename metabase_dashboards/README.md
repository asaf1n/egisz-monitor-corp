# Первичный набор дашбордов Metabase (Corp)

Каталог **целиком копируется в Docker-образ** (`../metabase/Dockerfile` → `/app/metabase_dashboards/`). Скрипт `../metabase/setup-dashboards.sh` (вызов из `../metabase/provision.sh` при старте пода) читает **все** `*.json` здесь и создаёт дашборды с вложенными native SQL-карточками в **корне** личной коллекции администратора Metabase.

Для смены отчётов правьте эти файлы, затем пересоберите образ `egisz-corp-metabase` и перезапустите Metabase, либо для локального цикла без пересборки k8s используйте `../metabase/provision-local.ps1` (примонтированный каталог `metabase_dashboards`).

| Файл | `name` в JSON (как в UI) |
|------|-------------------------|
| `01_operational.json` | Оперативный мониторинг (Corp) |
| `02_service.json` | Сервис интеграции (Corp) |
| `03_errors.json` | Ошибки и разбор (Corp) |
| `04_documents_no_response.json` | Документы без ответа (Corp) |
| `05_trends.json` | Динамика и тренды (Corp) |
| `06_quality.json` | Качество интеграции (Corp) |
| `07_errors_deep.json` | Глубокий анализ ошибок (Corp) |
| `08_pending_agg.json` | Аналитика зависших документов (Corp) |
| `09_executive.json` | Управленческий дашборд (Corp) |

Подробности: `../docs/METABASE.md`.
