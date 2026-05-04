# AGENTS.md — ориентир для ИИ-агентов по разработке

Репозиторий: **EGISZ Monitor Corp** — ETL и витрина для мониторинга обмена МИС ↔ ЕГИСЗ/РЭМД. Доменная логика парсинга, статусов, отчётов и поиска аномалий для аналитиков описана в **`.cursorrules`** (бизнес-контекст интеграции). Здесь — структура кода и инфраструктуры для правок функционала.

**После существенных изменений** (парсер, `sql/`, дашборды, K8s, `start.ps1`, Airflow DAG) **обновляй этот файл и `.cursorrules`**. `README.md` — витрина прототипа и навигация по дашбордам для сотрудников; технический контекст (ETL, курсоры, схема, инварианты, выкат) — здесь и в `.cursorrules`. **Тон и ограничения формулировок** — в разделе [«Стиль документации»](#стиль-документации) ниже; для агента в Cursor — `.cursor/rules/documentation-style.mdc`.

**Конец ответа в Cursor:** в **каждом** сообщении — отдельный **блок копирования** с **одной** конкретной командой **`.\start.ps1 -Action …`**, которую пользователь запускает, **чтобы применить внесённые в репозиторий правки к образам и подам** (это не описание последнего шага диалога и не замена выката). Без плейсхолдеров `<…>`. **`test`** в этот блок не ставить (pytest не обновляет образы). Детали — **`.cursorrules`** (раздел «Команда применения изменений»).

## Корень

| Путь | Назначение |
|------|------------|
| `pyproject.toml` | Пакет `egisz-monitor-corp`, зависимости; CLI: **`egisz-corp`** и **`egisz-monitor`** (оба → `egisz_monitor_corp.cli`) |
| `start.ps1` | Локальный стек в K8s (**namespace `egisz-monitor`**): по умолчанию **`apply`** / **`start`**; **`deploy`** — оба образа + DROP/CREATE БД приложения Metabase; **`reset-deploy`**, **`restart-metabase`** / **`restart-web`**, **`verify`** (port-forward + self-test Firebird, без `verify-corp-stack`), **`metabase-provision-local`**, **`test`** — см. **`docs/BI_EGISZ_INFOKLINIKA_AUDIT.md`** ([прил. B](docs/BI_EGISZ_INFOKLINIKA_AUDIT.md#приложение-b-развёртывание-и-эксплуатация)) и **`README.md`** (локальная инфраструктура). `apply`/`deploy`/`reset-deploy`: после in-cluster smoke **нет** вызова `verify-corp-stack.sh`. Пересборка conf-ui без кэша + apply: **`.\start.ps1 -Action apply -DockerNoCache`** или **`.\scripts\apply-local-rebuild.ps1`**. Сброс только app DB Metabase без удаления namespace: **`deploy`** / **`reset-deploy`** или env **`METABASE_FORCE_PROVISION`**. Скрипт в **UTF-8 с BOM**. |
| `README.md` | Витрина прототипа: что это за проект и какие дашборды доступны разным ролям |
| `.cursor/rules/documentation-style.mdc` | Стиль текстов для ИИ-агента Cursor (`alwaysApply`) — отсылает к **AGENTS.md** § «Стиль документации» |
| `.cursorrules` | Парсинг SOAP/XML, витрина, отчёты, критичные статусы и сигналы для мониторинга интеграции |
| `AGENTS.md` | Этот файл |

## Пакет Python `egisz_monitor_corp/`

| Модуль | Роль |
|--------|------|
| `cli.py` | CLI: `sync`, `apply-schema`, проверки БД |
| `etl.py` | **`run_sync`**: справочники первыми; **чередование** пакетов **EXCHANGELOG** (`LOGID > last_log_id`, без JOIN к FB `EGISZ_MESSAGES`) и страниц снимка **EGISZ_MESSAGES** в **`stg_egisz_messages_journal`** (keyset **`EGMID > after_egmid`**, стартовый `after` и фиксация после пакетов — **`etl_state.last_egmid`**); перед разбором пакета журнала — **догрузка** строк **`EGISZ_MESSAGES` по `MSGID`**, отсутствующих в staging (`egisz_messages_by_msgids_sql`); окно **`LOGDATE`/`CREATEDATE`** в Firebird только при **`sync_window_days` > 0**; при **0** — полный охват **за курсором** без дат; при **< 0** — «с нуля»: сброс **`last_log_id`/`last_egmid`**, `TRUNCATE` staging снимка; сопоставление журнала с сообщениями в PG по **`MSGID`**; UPSERT фактов; **исходящие** — полная перезапись **`stg_egisz_outbound_documents`** в том же окне **`CREATEDATE`**; прогресс UI: **`documents_*`** (uniq `localUid`/`emdrId`) и **`outbound_*_docs`** (uniq **`DOCUMENTID`**); без COUNT журнала в Firebird; `pg_try_advisory_lock` |
| `parser.py` | **`EgiszMonitorParser`**: разбор MSGTEXT (SOAP/XML), `relates_to_id`, `status`, `errors_json`, `localUid`, `emdr_id`, `resolve_clinic`; без **localUid**/DOCUMENTID и без **emdrId** — запись в **`stg_parse_errors`** (`MISSING_DOCUMENT_IDENTIFIERS`), не факт |
| `sql_util.py` | SQL к Firebird: **EXCHANGELOG** без JOIN (пагинация по **`LOGID`**); **EGISZ_MESSAGES**: keyset по **`EGMID`** для снимка, выборка по списку **`MSGID`** для догрузки; окно **`CREATEDATE`** и непустой **`DOCUMENTID`** — как у снимка, так и у исходящих |
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
| `002_etl_state.sql` | Таблица `etl_state`: `last_log_id`, **`last_egmid`**, кэш **`source_max_licenses_modifydate`** / **`source_peaks_updated_at`** после успешного ETL |
| `005_healthcheck.sql` | Healthcheck-витрина: `v_health_by_clinic` (агрегаты за 24ч по **уникальным** `relates_to_id`, очередь по **уникальным** `local_uid_semd`), `v_health_signals` (5 сигналов), `v_health_proxy_db` (сводка staging исходящих + курсор ETL) + UI-обёртки `*_ui` для блока healthcheck в `02_service.json` |

В каталоге `sql/` хранятся **только** DDL/витрина для Job `egisz-reports-schema-init` и ETL; **не** добавлять сюда одноразовые диагностические запросы. Предикаты выгрузки и курсоры — в **`egisz_monitor_corp/sql_util.py`** и в этом файле (**раздел ETL**).

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
| `metabase/provision.sh` | Старт пода: провижининг из `METABASE_DASHBOARDS_DIR` (по умолчанию `/app/metabase_dashboards/`); при `METABASE_FORCE_PROVISION=auto` пропуск повторного импорта только если число дашбордов в персональной коллекции не меньше числа JSON и **SHA набора всех `*.json`** совпадает с сохранённым после последнего успешного `setup-dashboards.sh` файлам `/shared/corp-metabase-dashboards-manifest.sha256` (переменная `METABASE_DASHBOARDS_MANIFEST_STAMP`) |
| `metabase/setup-dashboards.sh` | Очистка целевой коллекции администратора (дашборды, карточки, вложенные коллекции), затем импорт всех `*.json` из `DASHBOARDS_DIR` |
| `metabase/export_dashboards_from_api.py` | CLI-обёртка над **`egisz_monitor_corp.metabase_export`** (выгрузка в каталог) |
| `metabase/provision-local.ps1` | Локальный провижининг к Metabase на `localhost:3000` |
| `metabase/verify-corp-stack.sh` | Заглушка `exit 0` (в образе для совместимости); **не** вызывается из `provision.sh` / `start.ps1`. Диагностика вручную: `curl` `/api/health`, `smoke-metabase-ui.sh` |
| `metabase/force-k8s-mb-image.ps1` | Принудительное обновление образа Metabase в кластере при рассинхроне |

Подробности: **`docs/BI_EGISZ_INFOKLINIKA_AUDIT.md`** (§4 Metabase, прил. B), **`README.md`**.

## Kubernetes и окружение

Локальный сценарий из корня: **`.\start.ps1`** применяет манифесты в namespace **`egisz-monitor`** (см. `k8s/postgres/namespace.yaml`).

| Путь | Назначение |
|------|------------|
| `k8s/` | Манифесты Postgres, Metabase, conf-ui, CronJob, примеры секретов — см. **`README.md`** (локальная инфраструктура) и **`docs/BI_EGISZ_INFOKLINIKA_AUDIT.md`** (прил. B) |
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
| Config UI: вкладки **Snapshot / Healthcheck** | Snapshot — `GET /api/pg/sync-snapshot`: `last_log_id`, `last_egmid` (`etl_state`), кэш `MAX(MODIFYDATE)` лицензий; поле EGMID в UI заполняется тем же `last_egmid`. Healthcheck — сигналы, top-3 клиники, прокси-БД (`GET /api/healthcheck`, опрос ~30 c). **Сохранить в YAML** также патчит CronJob `egisz-monitor-sync` по `auto_sync` (RBAC SA `conf-ui`). |
| Дашборд `metabase_dashboards/02_service.json` | «02 Сервис, healthcheck и парсинг журнала»: поток по витрине (heatmap по **Обработано IPS** с тем же `dwh_date`, что топы и **06** архив); сигналы healthcheck, очередь, тренд парсинга с `parse_created_filter`; детальные карточки staging; сводка прокси-БД. |
| Полный аудит BI и интеграции | `docs/BI_EGISZ_INFOKLINIKA_AUDIT.md` (техника, витрина, Metabase, healthcheck, роли, k8s) |
| Операторам Config UI (дашборды vs веб, смысл метрик в отчётах) | `README.md`; технический лог синка и курсоры — **`AGENTS.md`** (раздел ETL), healthcheck — **`GET /api/healthcheck`** |

## ETL: pipeline и параллельный запуск

`etl.run_sync` расщеплён на чистые функции (см. `etl.py`):

- `_count_exchangelog_total` — заглушка: COUNT в Firebird для прогресса не выполняется.
- `_sync_journal_snapshot_interleaved` — основной цикл: пакет **EXCHANGELOG** → догрузка **`EGISZ_MESSAGES` по MSGID** при необходимости → разбор/UPSERT; затем страница снимка **EGISZ_MESSAGES** (`journal_messages_keyset_page_sql`); после исчерпания журнала — добор оставшихся страниц снимка. Поля метаданных сообщения для разбора берутся из PostgreSQL по **MSGID** (`stg_egisz_messages_journal`).
- `_process_exchangelog_pages` — упрощённый путь без PG (dry-run без staging): только страницы журнала.
- `_refresh_outbound_documents` — полная перезапись `stg_egisz_outbound_documents` из Firebird: те же отборы **`DOCUMENTID` + окно `CREATEDATE`**, что у снимка; в прогресс кладутся **`outbound_total`/`outbound_loaded`** (строки) и **`outbound_total_docs`/`outbound_loaded_docs`** (уникальные **`DOCUMENTID`**).

**Прогресс Config UI / `EtlProgressPayload`:** при разборе журнала — **`documents_unique`**, **`documents_localuid_unique`**, **`documents_emdrid_unique`** (уникальные значения из разобранного SOAP, не «строки журнала»).

**Single-flight на уровне БД**: в начале `run_sync` берём `pg_try_advisory_lock(hash(pipeline_name))`. Помеченный CronJob `egisz-monitor-sync` (по расписанию `*/15 * * * *`) и UI-кнопка «Синхронизировать сейчас» защищены от гонки — второй процесс выйдет с `PipelineLockBusyError` (CLI exit 75, UI показывает «параллельный sync уже идёт»). Lock — session-level: автоматически освобождается при разрыве соединения, без ручного reset после крэша воркера. Обновления `progress_detail_cb` при неизменной фазе троттлятся (~220 ms), при смене фазы — без задержки, чтобы UI не тормозил выборку и парсинг.

## Типичные задачи агента

1. **Поведение разбора / поля факта** — `parser.py`, `etl.py`, при необходимости `sql_util.py`; тесты в `tests/test_parser.py`.
2. **Витрина / подписи колонок** — `sql/001_schema.sql`; синхронизировать `dim_column_display_labels` и `*_ui` views.
3. **Дашборды** — править JSON в `metabase_dashboards/` (включая блок healthcheck в `02_service.json`), бамп тега Metabase в `start.ps1` + `k8s/metabase.yaml`, пересборка по **`docs/BI_EGISZ_INFOKLINIKA_AUDIT.md`** §4.
4. **Healthcheck** — `sql/005_healthcheck.sql`, `pg_warehouse.fetch_healthcheck_snapshot`, эндпоинт `/api/healthcheck`, JS-блок в `config_app.py`, карточки healthcheck в `02_service.json`. Пороги — комментарии к SQL и `docs/BI_EGISZ_INFOKLINIKA_AUDIT.md` §3.3.
5. **Диагностика синка и курсоры** — этот файл (**раздел ETL**) и **`egisz_monitor_corp/sql_util.py`**; снимок **`etl_state`** и healthcheck — Config UI / **`GET /api/healthcheck`**.

## Где что документировать

| Аудитория | Файл |
|-----------|------|
| Сотрудники: прототип, дашборды, единицы учёта в отчётах | **`README.md`** |
| ИИ-агенты и разработчики (структура репо, ETL, модули, выкат) | **`AGENTS.md`** |
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
