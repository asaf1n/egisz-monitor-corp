# Первичный набор дашбордов (EGISZ, Metabase)

Каталог **целиком копируется в Docker-образ** (`../metabase/Dockerfile` → `/app/metabase_dashboards/`). Скрипт `../metabase/setup-dashboards.sh` (вызов из `../metabase/provision.sh` при старте пода) читает **все** `*.json` и создаёт дашборды в **корне** личной коллекции администратора. Перед импортом в коллекции удаляются все существовавшие дашборды и сохранённые вопросы (см. `wipe_corp_root_collection` в `setup-dashboards.sh`).

| Файл | Имя дашборда в Metabase (`name`) |
|------|----------------------------------|
| `01_operational.json` | Оперативный мониторинг |
| `02_service.json` | Сервис интеграции |
| `03_errors.json` | Ошибки и разбор |
| `04_documents_no_response.json` | Документы без ответа |
| `05_trends.json` | Тренды и динамика |
| `06_quality.json` | Качество данных |
| `07_errors_deep.json` | Глубокий анализ ошибок |
| `08_pending_agg.json` | Агрегация ожидающих |
| `09_executive.json` | Управленческий дашборд |

Подробности: `../docs/METABASE.md`.
