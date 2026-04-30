# AGENTS.md — ориентир для ИИ-агентов по разработке

Репозиторий: **EGISZ Monitor Corp** — ETL и витрина для мониторинга обмена МИС ↔ ЕГИЗС/РЭМД. Доменная логика парсинга, статусов, отчётов и поиска аномалий для аналитиков описана в **`.cursorrules`** (бизнес-контекст интеграции). Здесь — структура кода и инфраструктуры для правок функционала.

**После существенных изменений** (парсер, `sql/`, дашборды, K8s, `start.ps1`, Airflow DAG) **обновляй этот файл и `.cursorrules`**. Для **`README.md`** держи ту же глубину, что и сейчас: правки — в логике текущих разделов, плюс навигация «слой за слоем» и сквозной поток (см. README).

## Корень

| Путь | Назначение |
|------|------------|
| `pyproject.toml` | Пакет `egisz-monitor-corp`, зависимости; CLI: **`egisz-corp`** и **`egisz-monitor`** (оба → `egisz_monitor_corp.cli`) |
| `start.ps1` | Локальный стек в K8s (**namespace `egisz-monitor`**): `deploy`, `reset-deploy`, `build`, `apply`, `reset-metabase`, `verify`, `web`, `forward`, `metabase-provision-local`, `test` |
| `README.md` | Обзор продукта, ETL, маппинг полей, таблица дашбордов Metabase |
| `.cursorrules` | Парсинг SOAP/XML, витрина, отчёты, критичные статусы и сигналы для мониторинга интеграции |
| `AGENTS.md` | Этот файл |

## Пакет Python `egisz_monitor_corp/`

| Модуль | Роль |
|--------|------|
| `cli.py` | CLI: `sync`, `apply-schema`, проверки БД |
| `etl.py` | **`run_sync`**: оркестрация Firebird → Postgres, батчи `EXCHANGELOG`, вызов парсера, UPSERT, outbound staging |
| `parser.py` | **`EgiszMonitorParser`**: разбор MSGTEXT (SOAP/XML), `relates_to_id`, `status`, `errors_json`, `localUid`, `resolve_clinic` |
| `sql_util.py` | SQL к Firebird: журнал, пагинация, обогащение `EGISZ_LICENSES` / `JPERSONS`, outbound |
| `pg_warehouse.py` | Подключение PG, применение `sql/*.sql`, `etl_state`, UPSERT факта/измерений |
| `fb_client.py` | Клиент Firebird |
| `config_loader.py` | Загрузка YAML (`EGISZ_CORP_CONFIG`, по умолчанию `config/egisz_corp.yaml`) |
| `config_app.py` | Flask-приложение конфиг-UI (если используется) |
| `sync_routes.py` | HTTP-ручки синка (single-flight), связка с `run_sync` |
| `semd_dictionary.py` | Справочник кодов СЭМД → наименования (fallback к `dim_semd_types`) |

## SQL и витрина (`sql/`)

| Файл | Содержимое |
|------|------------|
| `001_schema.sql` | Таблицы факта/измерений, представления `v_*`, функции `egisz_friendly_*`, `dim_column_display_labels` |
| `002_etl_state.sql` | Таблица `etl_state` (watermark `LOGID`) |
| `003_diagnostic_counts_firebird.sql` | Шаблоны диагностики FB |
| `004_diagnostic_counts_postgres.sql` | Шаблоны диагностики PG |

Схема на кластере: Job `egisz-reports-schema-init`, локально — `egisz-corp apply-schema`.

## Тесты (`tests/`)

`pytest`: `test_parser.py`, `test_sql_util.py`, `test_config_loader.py`. После изменений парсера или SQL — прогон тестов из корня репозитория.

## Metabase

| Путь | Назначение |
|------|------------|
| `metabase_dashboards/*.json` | Дашборды как код; имена и native-SQL карточек |
| `metabase_dashboards/README.md` | Соответствие файлов (`01_operational.json` … `10_errors_top.json`) и имён в UI |
| `metabase/Dockerfile` | Образ **`egisz-monitor-metabase`** (теги `:k8s-v9`, `:local` — см. `start.ps1 -Action build`) |
| `metabase/provision.sh` | Старт пода: провижининг из `/app/metabase_dashboards/` |
| `metabase/setup-dashboards.sh` | Импорт JSON в коллекцию администратора |
| `metabase/provision-local.ps1` | Локальный провижининг к Metabase на `localhost:3000` |
| `metabase/verify-corp-stack.sh` | Проверка состава образа (в т.ч. дашборды в образе) |
| `metabase/force-k8s-mb-image.ps1` | Принудительное обновление образа Metabase в кластере при рассинхроне |

Подробности: `docs/METABASE.md`.

## Kubernetes и окружение

Локальный сценарий из корня: **`.\start.ps1`** применяет манифесты в namespace **`egisz-monitor`** (см. `k8s/postgres/namespace.yaml`).

| Путь | Назначение |
|------|------------|
| `k8s/` | Обзор: [`k8s/README.md`](k8s/README.md) — Postgres, Metabase, conf-ui, примеры секретов |
| `k8s/postgres/` | StatefulSet, сервисы, Job **`egisz-reports-schema-init`** (`001_schema.sql`, `002_etl_state.sql`), Job’ы Metabase app DB и (при необходимости) Airflow metadata |
| `k8s/metabase.yaml` | Deployment Metabase (образ `egisz-monitor-metabase:k8s-v9`) |
| `k8s/conf-ui.yaml` | Config UI (Flask + `sync_routes`) |
| `k8s/local/egisz_corp.yaml` | Пример фрагмента конфига для секрета conf-ui |
| `k8s/airflow/` | Helm/values для DAG `egisz_corp_firebird_to_postgres` |
| `airflow/dags/egisz_corp_etl_dag.py` | Вызов `run_sync` |
| `docker/web/Dockerfile` | Образ **`egisz-conf-ui`** для Config UI |

## Конфигурация

- `config/egisz_corp.yaml` — рабочий конфиг (не коммитить секреты продакшена).
- `config/egisz_corp.example.yaml` — шаблон.

## Типичные задачи агента

1. **Поведение разбора / поля факта** — `parser.py`, `etl.py`, при необходимости `sql_util.py`; тесты в `tests/test_parser.py`.
2. **Витрина / подписи колонок** — `sql/001_schema.sql`; синхронизировать `dim_column_display_labels` и `*_ui` views.
3. **Дашборды** — править JSON в `metabase_dashboards/`, пересобирать образ Metabase / `provision-local` по `docs/METABASE.md`.
4. **Диагностика синка** — `docs/SYNC_DIAGNOSTICS.md`, SQL `003`/`004`.

Не дублируй в коде длинные доменные объяснения: для согласования терминов (СЭМД, `error` vs очередь без ответа) опирайся на **`.cursorrules`** и **`README.md`**.
