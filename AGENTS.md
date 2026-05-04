# AGENTS.md — ориентир для ИИ-агентов по разработке

Репозиторий: **EGISZ Monitor Corp** — ETL и витрина для мониторинга обмена МИС ↔ ЕГИЗС/РЭМД. Доменная логика парсинга, статусов, отчётов и поиска аномалий для аналитиков описана в **`.cursorrules`** (бизнес-контекст интеграции). Здесь — структура кода и инфраструктуры для правок функционала.

**После существенных изменений** (парсер, `sql/`, дашборды, K8s, `start.ps1`, Airflow DAG) **обновляй этот файл и `.cursorrules`**; операторские сценарии и сверка данных — в **`README.md`**. Для README держи ту же глубину: правки в логике текущих разделов, плюс навигация «слой за слоем» и сквозной поток. **Тон и ограничения формулировок** — в разделе [«Стиль документации»](#стиль-документации) ниже; для агента в Cursor — `.cursor/rules/documentation-style.mdc`.

**Конец ответа в Cursor:** в **каждом** сообщении — отдельный **блок копирования** с **одной** конкретной командой **`.\start.ps1 -Action …`**, которую пользователь запускает, **чтобы применить внесённые в репозиторий правки к образам и подам** (это не описание последнего шага диалога и не замена выката). Без плейсхолдеров `<…>`. **`test`** в этот блок не ставить (pytest не обновляет образы). Детали — **`.cursorrules`** (раздел «Команда применения изменений»).

## Корень

| Путь | Назначение |
|------|------------|
| `pyproject.toml` | Пакет `egisz-monitor-corp`, зависимости; CLI: **`egisz-corp`** и **`egisz-monitor`** (оба → `egisz_monitor_corp.cli`) |
| `start.ps1` | Локальный стек в K8s (**namespace `egisz-monitor`**): по умолчанию **`apply`** / **`start`**; **`deploy`** — оба образа + DROP/CREATE БД приложения Metabase; **`reset-deploy`**, **`restart-metabase`** / **`restart-web`**, **`verify`** (port-forward + self-test Firebird, без `verify-corp-stack`), **`metabase-provision-local`**, **`test`** — см. **`docs/BI_EGISZ_INFOKLINIKA_AUDIT.md`** [§8](docs/BI_EGISZ_INFOKLINIKA_AUDIT.md#k8s-local) и **`README.md`** (локальная инфраструктура). `apply`/`deploy`/`reset-deploy`: после in-cluster smoke **нет** вызова `verify-corp-stack.sh`. Пересборка conf-ui без кэша + apply: **`.\start.ps1 -Action apply -DockerNoCache`** или **`.\scripts\apply-local-rebuild.ps1`**. Сброс только app DB Metabase без удаления namespace: **`deploy`** / **`reset-deploy`** или env **`METABASE_FORCE_PROVISION`**. Скрипт в **UTF-8 с BOM**. |
| `README.md` | Обзор продукта, ETL, маппинг полей, дашборды Metabase, ручная диагностика синка |
| `.cursor/rules/documentation-style.mdc` | Стиль текстов для ИИ-агента Cursor (`alwaysApply`) — отсылает к **AGENTS.md** § «Стиль документации» |
| `.cursorrules` | Парсинг SOAP/XML, витрина, отчёты, критичные статусы и сигналы для мониторинга интеграции |
| `AGENTS.md` | Этот файл |

## Пакет Python `egisz_monitor_corp/`

| Модуль | Роль |
|--------|------|
| `cli.py` | CLI: `sync`, `apply-schema`, проверки БД |
| `etl.py` | **`run_sync`**: справочники первыми; чередование снимка **`EGISZ_MESSAGES`** → **`stg_egisz_messages_journal`** (FB→PG) и **EXCHANGELOG** по **`LOGID`**; при **`sync_window_days` <= 0** — без окна по **`LOGDATE`**/**`CREATEDATE`** в Firebird (все записи за курсорами) и полный пересъём staging снимка (**TRUNCATE** + сброс **`messages_snapshot_high_egmid`**); сопоставление **`MSGID`** в PostgreSQL; UPSERT фактов; outbound по **`DOCUMENTID`**/**`CREATEDATE`**; прогресс **`etl_last_egmid`**; без COUNT журнала в Firebird; `pg_try_advisory_lock` |
| `parser.py` | **`EgiszMonitorParser`**: разбор MSGTEXT (SOAP/XML), `relates_to_id`, `status`, `errors_json`, `localUid`, `resolve_clinic` |
| `sql_util.py` | SQL к Firebird: **EXCHANGELOG** без JOIN (пагинация по **`LOGID`**); **EGISZ_MESSAGES** для снимка журнала (FIRST/SKIP, окно **`CREATEDATE`**, непустой **`DOCUMENTID`**); исходящие для staging; диагностический **`MSGID IN`** |
| `pg_warehouse.py` | Подключение PG, применение `sql/*.sql`, `etl_state`, staging исходящих, UPSERT факта чанками / измерений |
| `fb_client.py` | Клиент Firebird |
| `config_loader.py` | Загрузка YAML (`EGISZ_MONITOR_CONFIG`, по умолчанию `config/egisz_monitor.yaml`) |
| `config_app.py` | Flask-приложение конфиг-UI: сохранение YAML, sync, healthcheck, **POST `/api/metabase/export-dashboards-json`** (ZIP дашбордов Metabase) |
| `metabase_export.py` | Выгрузка дашбордов Metabase в JSON (формат `metabase_dashboards/`); используется Config UI и CLI |
| `sync_routes.py` | HTTP-ручки синка (single-flight), связка с `run_sync` |
| `semd_dictionary.py` | Справочник кодов СЭМД → наименования (fallback к `dim_semd_types`) |

## SQL и витрина (`sql/`)

| Файл | Содержимое |
|------|------------|
| `001_schema.sql` | Таблицы факта/измерений, `stg_jpersons_import`, `stg_egisz_licenses_import`, **`stg_egisz_messages_journal`**, `stg_egisz_outbound_documents`, представления `v_*`, **`v_stg_parse_errors_by_document`** (ключ документа для ошибок парсинга), функции `egisz_friendly_*`, `dim_column_display_labels` |
| `002_etl_state.sql` | Таблица `etl_state`: `last_log_id`; `last_egmid` (журнал); **`messages_snapshot_high_egmid`** (инкремент снимка **EGISZ_MESSAGES**); пики FB |
| `005_healthcheck.sql` | Healthcheck-витрина: `v_health_by_clinic` (агрегаты за 24ч по **уникальным** `relates_to_id`, очередь по **уникальным** `local_uid_semd`), `v_health_signals` (5 сигналов), `v_health_proxy_db` (сводка staging исходящих + курсор ETL) + UI-обёртки `*_ui` для блока healthcheck в `02_service.json` |

В каталоге `sql/` хранятся **только** DDL/витрина для Job `egisz-reports-schema-init` и ETL; **не** добавлять сюда одноразовые диагностические запросы для DBeaver — операторская сверка курсора и объёмов описана в **`README.md`** (раздел «Ручная диагностика синка»).

Схема на кластере: Job `egisz-reports-schema-init` (порядок файлов — `sql/schema_apply_order.txt`, по умолчанию `001 + 002 + 005`), локально — `egisz-monitor apply-schema`. ETL `run_sync` идемпотентно применяет тот же набор при каждом запуске.

## Тесты (`tests/`)

`pytest`: `test_parser.py`, `test_sql_util.py`, `test_config_loader.py`, `test_fb_client.py`, `test_pg_warehouse.py` (мок healthcheck-снапшота PG), `test_config_app.py` (Flask test client для `/api/healthcheck`), `test_etl_helpers.py` (хелперы `run_sync`, advisory lock в Postgres). После изменений парсера, SQL или эндпоинтов — прогон тестов из корня (`start.ps1 -Action test` или `pytest`).

## Metabase

Агрегаты в `metabase_dashboards/*.json`: **документная единица** — `COUNT(DISTINCT "Связанное сообщение")` на витрине колбэков (`relates_to_id`), для очереди без ответа — `COUNT(DISTINCT "localUid СЭМД")`; см. **`docs/BI_EGISZ_INFOKLINIKA_AUDIT.md`** §5 и **`README.md`** (Metabase).

| Путь | Назначение |
|------|------------|
| `metabase_dashboards/*.json` | Дашборды как код; имена и native-SQL карточек |

**Файл JSON → имя дашборда в Metabase (`name`):**

| Файл | Имя в UI |
|------|----------|
| `01_operational.json` | 01 Оперативный мониторинг и динамика |
| `02_service.json` | 02 Сервис, healthcheck и парсинг журнала |
| `03_documents_no_response.json` | 03 Документы без ответа |
| `04_quality_and_errors.json` | 04 Ошибки и качество данных |
| `05_executive.json` | 05 Управление СЭМД |
| `06_semd_archive.json` | 06 Архив СЭМД |

Карточки на дашбордах **02–05** в JSON: префикс **`NN ·`** в `name` для уникальности в общей коллекции; **01** — часть карточек без префикса (оперативный блок).

| Путь | Назначение |
|------|------------|
| `metabase/Dockerfile` | Образ **`egisz-monitor-metabase`** (теги `:k8s-v23`, `:local` — `docker build` в `start.ps1` deploy/reset-deploy/restart-metabase и в `metabase/provision-local.ps1`; non-root UID 1500, **python3** + `PYTHONPATH=/app` для `python3 -m egisz_monitor_corp.metabase_export`, HEALTHCHECK; bump при смене JSON/скриптов) |
| `metabase/provision.sh` | Старт пода: провижининг из `/app/metabase_dashboards/`; при `METABASE_FORCE_PROVISION=auto` и достаточном числе дашбордов пропуск импорта возможен только если native SQL карточки **«Последние операции»** (дашборд 01) содержит тот же якорь, что в образе (фрагмент запроса к `v_egisz_transactions_enriched_ui`, см. `corp_mb_native_sql_anchor_matches_image` в скрипте) — иначе после правок JSON без DROP БД Metabase остался бы старый SQL |
| `metabase/setup-dashboards.sh` | Импорт JSON в коллекцию администратора |
| `metabase/export_dashboards_from_api.py` | CLI-обёртка над **`egisz_monitor_corp.metabase_export`** (выгрузка в каталог) |
| `metabase/provision-local.ps1` | Локальный провижининг к Metabase на `localhost:3000` |
| `metabase/verify-corp-stack.sh` | Заглушка `exit 0` (в образе для совместимости); **не** вызывается из `provision.sh` / `start.ps1`. Диагностика вручную: `curl` `/api/health`, `smoke-metabase-ui.sh` |
| `metabase/force-k8s-mb-image.ps1` | Принудительное обновление образа Metabase в кластере при рассинхроне |

Подробности: **`docs/BI_EGISZ_INFOKLINIKA_AUDIT.md`** (§4 Metabase, §8 Kubernetes), **`README.md`**.

## Kubernetes и окружение

Локальный сценарий из корня: **`.\start.ps1`** применяет манифесты в namespace **`egisz-monitor`** (см. `k8s/postgres/namespace.yaml`).

| Путь | Назначение |
|------|------------|
| `k8s/` | Манифесты Postgres, Metabase, conf-ui, CronJob, примеры секретов — см. **`README.md`** (локальная инфраструктура) и BI-аудит §8.6 |
| `k8s/postgres/` | StatefulSet, сервисы, Job **`egisz-reports-schema-init`** (DDL по `sql/schema_apply_order.txt`), Job’ы Metabase app DB и (при необходимости) Airflow metadata |
| `k8s/metabase.yaml` | Deployment Metabase (образ `egisz-monitor-metabase:k8s-v23`); `JAVA_TOOL_OPTIONS` (G1GC, MaxRAMPercentage, `ExitOnOutOfMemoryError`), длинный `startupProbe` до `/api/health`, `METABASE_FORCE_PROVISION=auto` — см. `metabase/provision.sh` |
| `k8s/conf-ui.yaml` | Config UI (gunicorn 1×16t + `sync_routes`); non-root UID 10001, `/healthz`, RollingUpdate `maxUnavailable=0` |
| `k8s/etl-cron.yaml` | **CronJob `egisz-monitor-sync`**: `*/15 * * * *`, тот же образ `egisz-conf-ui:sync-web`, CLI `egisz-monitor sync`, `concurrencyPolicy: Forbid` + advisory lock в Postgres против гонки с UI-кнопкой. **`spec.suspend` / `schedule` / `timeZone`** выравниваются с **`auto_sync`** в YAML (POST /save в Config UI, `egisz-monitor k8s-reconcile-cronjob`, шаг `start.ps1` после apply). В манифесте стартово `suspend: true`. |
| `k8s/local/egisz_monitor.yaml` | Пример фрагмента конфига для секрета conf-ui |
| `k8s/airflow/` | Helm/values для DAG `egisz_monitor_firebird_to_postgres` |
| `airflow/dags/egisz_monitor_etl_dag.py` | Вызов `run_sync` |
| `docker/web/Dockerfile` | Образ **`egisz-conf-ui`** для Config UI |

## Docker, Kubernetes и CI (краткая справка)

- **Сборка образов** (контекст — корень репозитория):  
  `docker build -f metabase/Dockerfile -t egisz-monitor-metabase:local .`  
  `docker build -f docker/web/Dockerfile -t egisz-conf-ui:sync-web .`  
  Для web при необходимости: `--build-arg PYTHON_BASE=…`, `--build-arg PIP_INDEX_URL=…` (см. комментарии в `docker/web/Dockerfile`).
- **Манифесты** (при настроенном `kubectl` и namespace `egisz-monitor`): `kubectl apply -f k8s/metabase.yaml`, `kubectl apply -f k8s/conf-ui.yaml`; наблюдение за стартом Metabase: `kubectl get pods -w -n egisz-monitor -l app.kubernetes.io/name=metabase`.
- **CI**: `.github/workflows/docker-build-scan.yml` — сборка с кэшем, сканирование Trivy, проверка Kubernetes YAML (детали — в workflow).
- **Смена версии базового Metabase** в `metabase/Dockerfile`: обновить тег в `FROM`, пересобрать образ, взять digest из `docker inspect <image> --format='{{json .RepoDigests}}'` и закрепить в виде `FROM …@sha256:…`; актуальные теги: [Docker Hub — metabase/metabase](https://hub.docker.com/r/metabase/metabase/tags).
- **Частые сбои**: у conf-ui при `readOnlyRootFilesystem` смотреть `volumeMounts` на `/tmp`, `/run`, `/app/config` в `k8s/conf-ui.yaml`; холодный старт JVM Metabase — укладываться в `startupProbe` в `k8s/metabase.yaml`; залипший кэш builder — `docker builder prune -a` и пересборка.
- **Перед merge** (минимум): оба `docker build` проходят; при включённом CI — зелёные job'ы; локальный smoke: контейнер conf-ui и `curl` на `/healthz` (ожидается 200).

## Конфигурация

- `config/egisz_monitor.yaml` — рабочий конфиг (не коммитить секреты продакшена).
- `config/egisz_monitor.example.yaml` — шаблон.

## Healthcheck интеграции

| Артефакт | Назначение |
|----------|------------|
| `sql/005_healthcheck.sql` | Витрины `v_health_by_clinic` / `v_health_signals` / `v_health_proxy_db` + UI-обёртки `*_ui` (русские подписи через `dim_column_display_labels`). Входит в `schema_apply_order.txt`; применяется ETL `run_sync` и Job `egisz-reports-schema-init`. |
| `egisz_monitor_corp/pg_warehouse.py` → `fetch_healthcheck_snapshot(con)` | Чтение трёх view + агрегаты для UI/JSON; `statement_timeout = 10s` на каждый блок. |
| `GET /api/healthcheck` (`egisz_monitor_corp/config_app.py`) | JSON-снимок `{signals, by_clinic_top, proxy_db, level_summary}`. При недоступной PG — `ok: false` и `errors[]` (graceful). |
| Config UI: вкладки **Snapshot / Healthcheck** | Snapshot — текущие `EGMID/LOGID/MODIFYDATE` (как раньше). Healthcheck — сигналы, top-3 клиники, прокси-БД (опрос `/api/healthcheck` каждые 30 c). **Сохранить в YAML** также патчит CronJob `egisz-monitor-sync` по `auto_sync` (RBAC SA `conf-ui`). |
| Дашборд `metabase_dashboards/02_service.json` | «02 Сервис, healthcheck и парсинг журнала»: поток по витрине; сигналы, heatmap, очередь, тренд парсинга; детальные карточки staging (`v_stg_parse_errors_by_document`, фильтр `parse_created_filter`); сводка прокси-БД. |
| Полный аудит BI и интеграции | `docs/BI_EGISZ_INFOKLINIKA_AUDIT.md` (техника, витрина, Metabase, healthcheck, роли, k8s) |
| Операторам Config UI (лог, прогресс, курсоры, Metabase vs веб) | `README.md` → [Синхронизация Firebird → PostgreSQL](README.md#синхронизация-firebird--postgresql) |

## ETL: pipeline и параллельный запуск

`etl.run_sync` расщеплён на чистые функции (см. `etl.py`):

- `_count_exchangelog_total` — заглушка: COUNT в Firebird для прогресса не выполняется.
- `_process_exchangelog_pages` — постраничный **EXCHANGELOG** из Firebird; кэш полей сообщения по **MSGID** из PostgreSQL (`stg_egisz_messages_journal`); парсинг и UPSERT.
- `_refresh_outbound_documents` — полная перезапись `stg_egisz_outbound_documents` из Firebird: `EGISZ_MESSAGES` с `DOCUMENTID` в том же окне **`CREATEDATE`**, что и журнал (`sync_window_days`); сортировка `EGMID DESC`, одна строка на `DOCUMENTID`.

**Single-flight на уровне БД**: в начале `run_sync` берём `pg_try_advisory_lock(hash(pipeline_name))`. Помеченный CronJob `egisz-monitor-sync` (по расписанию `*/15 * * * *`) и UI-кнопка «Синхронизировать сейчас» защищены от гонки — второй процесс выйдет с `PipelineLockBusyError` (CLI exit 75, UI показывает «параллельный sync уже идёт»). Lock — session-level: автоматически освобождается при разрыве соединения, без ручного reset после крэша воркера. Обновления `progress_detail_cb` при неизменной фазе троттлятся (~220 ms), при смене фазы — без задержки, чтобы UI не тормозил выборку и парсинг.

## Типичные задачи агента

1. **Поведение разбора / поля факта** — `parser.py`, `etl.py`, при необходимости `sql_util.py`; тесты в `tests/test_parser.py`.
2. **Витрина / подписи колонок** — `sql/001_schema.sql`; синхронизировать `dim_column_display_labels` и `*_ui` views.
3. **Дашборды** — править JSON в `metabase_dashboards/` (включая блок healthcheck в `02_service.json`), бамп тега Metabase в `start.ps1` + `k8s/metabase.yaml`, пересборка по **`docs/BI_EGISZ_INFOKLINIKA_AUDIT.md`** §4.
4. **Healthcheck** — `sql/005_healthcheck.sql`, `pg_warehouse.fetch_healthcheck_snapshot`, эндпоинт `/api/healthcheck`, JS-блок в `config_app.py`, карточки healthcheck в `02_service.json`. Пороги — комментарии к SQL и `docs/BI_EGISZ_INFOKLINIKA_AUDIT.md` §3.3.
5. **Диагностика синка / курсор для операторов** — только **`README.md`** (раздел «Ручная диагностика синка»); предикаты ETL — **`egisz_monitor_corp/sql_util.py`**.

## Где что документировать

| Аудитория | Файл |
|-----------|------|
| Операторы, аналитики, обзор продукта и ETL | **`README.md`** |
| ИИ-агенты и разработчики (структура репо, модули, типовые задачи) | **`AGENTS.md`** |
| Домен интеграции, парсинг, сигналы, healthcheck, правила ответа в Cursor | **`.cursorrules`** |
| Аудит BI + интеграция МИС «Инфоклиника» ↔ ЕГИСЗ (единый том) | **`docs/BI_EGISZ_INFOKLINIKA_AUDIT.md`** |

<a id="doc-style"></a>

## Стиль документации

Тексты в **`README.md`**, **`docs/BI_EGISZ_INFOKLINIKA_AUDIT.md`**, **`AGENTS.md`**, **`.cursorrules`** читают инженеры вне контекста чата. Соблюдай:

1. **Конструктивное описание** — что система делает и откуда данные; не раздувай перечислением того, чего «нет», кроме жёстких требований безопасности/совместимости.
2. **Аудитория** — человек, впервые открывший репозиторий: достаточно доменного контекста без отсылок к переписке.
3. **Без цитирования чата** — итог читается как справочная статья, не как лог мессенджера.
4. **Согласованность терминов** — один смысл для MSGTEXT, LOGTEXT, JID и т.д.; при смене поведения в коде обновляй связанные разделы README, аудита, AGENTS и `.cursorrules`.

Длинные доменные пояснения **не** дублируй в коде комментариями: для согласования терминов (СЭМД, `error` vs очередь без ответа) опирайся на **`.cursorrules`** и **`README.md`**.
