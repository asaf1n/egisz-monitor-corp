# AGENTS.md — ориентир для ИИ-агентов по разработке

Репозиторий: **EGISZ Monitor Corp** — ETL и витрина для мониторинга обмена МИС ↔ ЕГИЗС/РЭМД. Доменная логика парсинга, статусов, отчётов и поиска аномалий для аналитиков описана в **`.cursorrules`** (бизнес-контекст интеграции). Здесь — структура кода и инфраструктуры для правок функционала.

**После существенных изменений** (парсер, `sql/`, дашборды, K8s, `start.ps1`, Airflow DAG) **обновляй этот файл и `.cursorrules`**. Для **`README.md`** держи ту же глубину, что и сейчас: правки — в логике текущих разделов, плюс навигация «слой за слоем» и сквозной поток (см. README).

## Корень

| Путь | Назначение |
|------|------------|
| `pyproject.toml` | Пакет `egisz-monitor-corp`, зависимости; CLI: **`egisz-corp`** и **`egisz-monitor`** (оба → `egisz_monitor_corp.cli`) |
| `start.ps1` | Локальный стек в K8s (**namespace `egisz-monitor`**): по умолчанию **`apply`** / **`start`** (без сброса БД Metabase); **`deploy`** — полная пересборка образов + DROP/CREATE БД Metabase; также `reset-deploy`, `build`, `apply-rebuild`, `restart-*`, `reset-metabase`, `verify`, `web`, `forward`, `metabase-provision-local`, `test` — см. **`docs/KUBERNETES_LOCAL.md`** |
| `README.md` | Обзор продукта, ETL, маппинг полей, таблица дашбордов Metabase |
| `.cursorrules` | Парсинг SOAP/XML, витрина, отчёты, критичные статусы и сигналы для мониторинга интеграции |
| `AGENTS.md` | Этот файл |

## Пакет Python `egisz_monitor_corp/`

| Модуль | Роль |
|--------|------|
| `cli.py` | CLI: `sync`, `apply-schema`, проверки БД |
| `etl.py` | **`run_sync`**: JPERSONS и EGISZ_LICENSES из FB по отдельности → `stg_jpersons_import` + `stg_egisz_licenses_import`, сшивка JNAME/JINN/FIR_OID в **PostgreSQL** (`UPDATE … FROM`), merge в `dim_clinics`; чередование пакетов по 65k **EGISZ_MESSAGES** (EGMID) и **EXCHANGELOG** (LOGID) с дозагрузкой по MSGID и фоновым SELECT следующей страницы сообщений на время парсинга журнала; без COUNT в Firebird; UPSERT фактов **чанками** (`facts_upsert_chunk_size`, опционально `pg_upsert_statement_timeout_sec`); outbound по `EGMID` |
| `parser.py` | **`EgiszMonitorParser`**: разбор MSGTEXT (SOAP/XML), `relates_to_id`, `status`, `errors_json`, `localUid`, `resolve_clinic` |
| `sql_util.py` | SQL к Firebird: `EXCHANGELOG` и пагинация по `LOGID` (до 65k); инкремент `EGISZ_MESSAGES` по `EGMID` (до 65k); выборка по списку `MSGID`; полный `JPERSONS`; полная выборка `EGISZ_LICENSES` без JOIN; outbound по минимальному `EGMID` |
| `pg_warehouse.py` | Подключение PG, применение `sql/*.sql`, `etl_state`, staging JPERSONS/лицензий, UPSERT факта чанками / измерений |
| `fb_client.py` | Клиент Firebird |
| `config_loader.py` | Загрузка YAML (`EGISZ_MONITOR_CONFIG`, по умолчанию `config/egisz_monitor.yaml`) |
| `config_app.py` | Flask-приложение конфиг-UI (если используется) |
| `sync_routes.py` | HTTP-ручки синка (single-flight), связка с `run_sync` |
| `semd_dictionary.py` | Справочник кодов СЭМД → наименования (fallback к `dim_semd_types`) |

## SQL и витрина (`sql/`)

| Файл | Содержимое |
|------|------------|
| `001_schema.sql` | Таблицы факта/измерений, `stg_jpersons_import`, `stg_egisz_licenses_import`, `stg_egisz_outbound_documents`, представления `v_*`, функции `egisz_friendly_*`, `dim_column_display_labels` |
| `002_etl_state.sql` | Таблица `etl_state`: курсоры `last_log_id` (EXCHANGELOG.LOGID), `last_egmid` (EGISZ_MESSAGES.EGMID), пики FB |
| `003_diagnostic_counts_firebird.sql` | Шаблоны диагностики FB |
| `004_diagnostic_counts_postgres.sql` | Шаблоны диагностики PG |
| `005_healthcheck.sql` | Healthcheck-витрина: `v_health_by_clinic` (агрегаты за 24ч), `v_health_signals` (5 сигналов), `v_health_proxy_db` (сводка staging исходящих + курсор ETL) + UI-обёртки `*_ui` для дашборда `11_healthcheck.json` |
| `006_firebird_proxy_export.sql` | Ручные выгрузки с прокси Firebird (пики, outbound, журнал 65k / инкремент после `LOGID`); не применяется Job'ом схемы |

Схема на кластере: Job `egisz-reports-schema-init` (порядок файлов — `sql/schema_apply_order.txt`, по умолчанию `001 + 002 + 005`), локально — `egisz-monitor apply-schema`. ETL `run_sync` идемпотентно применяет тот же набор при каждом запуске.

## Тесты (`tests/`)

`pytest`: `test_parser.py`, `test_sql_util.py`, `test_config_loader.py`, `test_fb_client.py`, `test_pg_warehouse.py` (мок healthcheck-снапшота PG), `test_config_app.py` (Flask test client для `/api/healthcheck`), `test_etl_helpers.py` (хелперы `run_sync`, advisory lock в Postgres). После изменений парсера, SQL или эндпоинтов — прогон тестов из корня (`start.ps1 -Action test` или `pytest`).

## Metabase

| Путь | Назначение |
|------|------------|
| `metabase_dashboards/*.json` | Дашборды как код; имена и native-SQL карточек |
| `metabase_dashboards/README.md` | Соответствие файлов (`01_operational.json` … `11_healthcheck.json`) и имён в UI |
| `metabase/Dockerfile` | Образ **`egisz-monitor-metabase`** (теги `:k8s-v15`, `:local` — см. `start.ps1 -Action build`; non-root UID 1500, multi-stage `--virtual` apk-deps, HEALTHCHECK; bump при смене JSON/скриптов) |
| `metabase/provision.sh` | Старт пода: провижининг из `/app/metabase_dashboards/` |
| `metabase/setup-dashboards.sh` | Импорт JSON в коллекцию администратора |
| `metabase/provision-local.ps1` | Локальный провижининг к Metabase на `localhost:3000` |
| `metabase/verify-corp-stack.sh` | Проверка состава образа (в т.ч. дашборды в образе) |
| `metabase/force-k8s-mb-image.ps1` | Принудительное обновление образа Metabase в кластере при рассинхроне |

Подробности: **`docs/KUBERNETES_LOCAL.md`** (kubectl и сценарии `start.ps1`), **`docs/METABASE.md`**.

## Kubernetes и окружение

Локальный сценарий из корня: **`.\start.ps1`** применяет манифесты в namespace **`egisz-monitor`** (см. `k8s/postgres/namespace.yaml`).

| Путь | Назначение |
|------|------------|
| `k8s/` | Обзор: [`k8s/README.md`](k8s/README.md) — Postgres, Metabase, conf-ui, примеры секретов |
| `k8s/postgres/` | StatefulSet, сервисы, Job **`egisz-reports-schema-init`** (DDL по `sql/schema_apply_order.txt`), Job’ы Metabase app DB и (при необходимости) Airflow metadata |
| `k8s/metabase.yaml` | Deployment Metabase (образ `egisz-monitor-metabase:k8s-v15`); `JAVA_TOOL_OPTIONS=-XX:MaxRAMPercentage=75 -XX:+UseG1GC`, startupProbe (240s), `METABASE_FORCE_PROVISION=auto` (идемпотентный provision — dashboard ID не меняются) |
| `k8s/conf-ui.yaml` | Config UI (gunicorn 1×16t + `sync_routes`); non-root UID 10001, `/healthz`, RollingUpdate `maxUnavailable=0` |
| `k8s/etl-cron.yaml` | **CronJob `egisz-monitor-sync`**: `*/15 * * * *`, тот же образ `egisz-conf-ui:sync-web`, CLI `egisz-monitor sync`, `concurrencyPolicy: Forbid` + advisory lock в Postgres против гонки с UI-кнопкой |
| `k8s/local/egisz_monitor.yaml` | Пример фрагмента конфига для секрета conf-ui |
| `k8s/airflow/` | Helm/values для DAG `egisz_monitor_firebird_to_postgres` |
| `airflow/dags/egisz_monitor_etl_dag.py` | Вызов `run_sync` |
| `docker/web/Dockerfile` | Образ **`egisz-conf-ui`** для Config UI |

## Конфигурация

- `config/egisz_monitor.yaml` — рабочий конфиг (не коммитить секреты продакшена).
- `config/egisz_monitor.example.yaml` — шаблон.

## Healthcheck интеграции

| Артефакт | Назначение |
|----------|------------|
| `sql/005_healthcheck.sql` | Витрины `v_health_by_clinic` / `v_health_signals` / `v_health_proxy_db` + UI-обёртки `*_ui` (русские подписи через `dim_column_display_labels`). Входит в `schema_apply_order.txt`; применяется ETL `run_sync` и Job `egisz-reports-schema-init`. |
| `egisz_monitor_corp/pg_warehouse.py` → `fetch_healthcheck_snapshot(con)` | Чтение трёх view + агрегаты для UI/JSON; `statement_timeout = 10s` на каждый блок. |
| `GET /api/healthcheck` (`egisz_monitor_corp/config_app.py`) | JSON-снимок `{signals, by_clinic_top, proxy_db, level_summary}`. При недоступной PG — `ok: false` и `errors[]` (graceful). |
| Config UI: вкладки **Snapshot / Healthcheck** | Snapshot — текущие `EGMID/LOGID/MODIFYDATE` (как раньше). Healthcheck — сигналы, top-3 клиники, прокси-БД (опрос `/api/healthcheck` каждые 30 c). |
| Дашборд `metabase_dashboards/11_healthcheck.json` | «11 Healthcheck интеграции»: сигналы, heatmap клиник × дни, age-buckets очереди, тренд parse-errors, сводка прокси-БД. |
| Полный аудит | `docs/INTEGRATION_AUDIT.md` (3 фокуса: техника/бизнес/healthcheck). |
| Операторам Config UI (лог, прогресс, курсоры, Metabase vs веб) | `README.md` → [Синхронизация Firebird → PostgreSQL](README.md#синхронизация-firebird--postgresql) |

## ETL: pipeline и параллельный запуск

`etl.run_sync` расщеплён на чистые функции (см. `etl.py`):

- `_export_egisz_licenses_full` — `JPERSONS`, затем `EGISZ_LICENSES` без JOIN в Firebird; в PG — два staging, **`UPDATE … FROM`** для полей юрлица, merge в `dim_clinics`, выборка для кэша ETL из PostgreSQL; при `dry_run` без PG — сшивка в Python.
- `_count_exchangelog_total` — заглушка: COUNT в Firebird для прогресса не выполняется.
- `_run_interleaved_messages_and_journal` — чередование пакетов по 65k: сообщения по `EGMID`, журнал по `LOGID`; дозагрузка `EGISZ_MESSAGES` по недостающим `MSGID` страницы журнала; `ThreadPoolExecutor`: следующая страница сообщений читается из FB, пока парсится текущая страница журнала; `last_egmid` — после успешного прогона.
- `_process_exchangelog_pages` — полный проход журнала с уже загруженным `msg_by_msgid` (тесты и совместимость).
- `_refresh_outbound_documents` — снимок `stg_egisz_outbound_documents` из Firebird по `EGMID` выше курсора на начало прогона.

**Single-flight на уровне БД**: в начале `run_sync` берём `pg_try_advisory_lock(hash(pipeline_name))`. Помеченный CronJob `egisz-monitor-sync` (по расписанию `*/15 * * * *`) и UI-кнопка «Синхронизировать сейчас» защищены от гонки — второй процесс выйдет с `PipelineLockBusyError` (CLI exit 75, UI показывает «параллельный sync уже идёт»). Lock — session-level: автоматически освобождается при разрыве соединения, без ручного reset после крэша воркера. Обновления `progress_detail_cb` при неизменной фазе троттлятся (~220 ms), при смене фазы — без задержки, чтобы UI не тормозил выборку и парсинг.

## Типичные задачи агента

1. **Поведение разбора / поля факта** — `parser.py`, `etl.py`, при необходимости `sql_util.py`; тесты в `tests/test_parser.py`.
2. **Витрина / подписи колонок** — `sql/001_schema.sql`; синхронизировать `dim_column_display_labels` и `*_ui` views.
3. **Дашборды** — править JSON в `metabase_dashboards/` (включая `11_healthcheck.json`), бамп тега Metabase в `start.ps1` + `k8s/metabase.yaml`, пересборка по `docs/METABASE.md`.
4. **Healthcheck** — `sql/005_healthcheck.sql`, `pg_warehouse.fetch_healthcheck_snapshot`, эндпоинт `/api/healthcheck`, JS-блок в `config_app.py`, JSON `11_healthcheck.json`. Пороги — комментарии к SQL и `docs/INTEGRATION_AUDIT.md` §3.3.
5. **Диагностика синка** — `docs/SYNC_DIAGNOSTICS.md`, SQL `003`/`004`.

Не дублируй в коде длинные доменные объяснения: для согласования терминов (СЭМД, `error` vs очередь без ответа) опирайся на **`.cursorrules`** и **`README.md`**.
