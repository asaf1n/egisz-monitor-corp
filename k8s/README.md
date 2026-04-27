# Kubernetes (`k8s/`)

Каталог содержит манифесты для namespace **`egisz-corp`**: PostgreSQL (витрина), Metabase (аналитика + провижининг дашбордов), Config UI, Job’ы схемы SQL. **Отдельный Postgres в Docker Compose из репозитория убран** — витрина только в кластере; образы `egisz-conf-ui` и `egisz-corp-metabase` по-прежнему собираются **docker build** на хосте и подгружаются в kind / используются Docker Desktop K8s. Типовой сценарий: **`.\start.ps1 -Action deploy`** или **`reset-deploy`** (полный сброс: удаление старого compose-тома при наличии, `--no-cache` сборка образов, пересоздание namespace). Подробности: [`docs/METABASE.md`](../docs/METABASE.md).

| Путь / объект | Назначение |
| :--- | :--- |
| `namespace.yaml` | Namespace `egisz-corp` |
| `postgres/*` | StatefulSet Postgres, сервисы, Job `egisz-reports-schema-init` (`sql/001_schema.sql`, `002_etl_state.sql`) |
| `metabase.yaml` | Deployment Metabase (`egisz-corp-metabase:local`, см. ниже), Service (NodePort 30300, `metabase-lb`), `hostPort` 3000 для `http://127.0.0.1:3000/` |
| `metabase-admin-secret*.yaml` | Учётка администратора API/UI Metabase (шаблон `*.example`) |
| `conf-ui.yaml` | Config UI (ETL-конфиг) |
| `local/egisz_corp.yaml` | Пример конфигурации для секрета `egisz-corp-conf-ui-config` (локальная отладка) |
| `airflow/` | Отдельно: Helm/Airflow (см. `k8s/airflow/README.md`) |

**Образ Metabase в кластере:** `egisz-corp-metabase:local`, `imagePullPolicy: IfNotPresent` — собирается локально (`metabase/Dockerfile`, `.\start.ps1 -Action build` создаёт тег `:local` после `latest`). Если в поде **нет** `/app/verify-corp-stack.sh`: `.\metabase\force-k8s-mb-image.ps1` или `build` + `apply`/`deploy`. Старый сценарий с одним только `:latest` в манифесте давал устаревший digest в кэше ноды.

---

## EGISZ Monitor Corp (контекст ETL)

**EGISZ Monitor Corp** — корпоративный ETL для мониторинга обмена МИС с реестрами ЕГИСЗ: Firebird → парсинг SOAP → витрины в PostgreSQL.

### Выборка данных (Sampling)
* **Watermark:** `EXCHANGELOG.LOGID` → `etl_state.last_log_id` в PostgreSQL.
* **Окно:** `LOGDATE` в границах `sync_window_days` (см. конфиг).
* **Пакеты:** `SELECT FIRST {batch_size} … ORDER BY LOGID`.

### Кэширование
Справочники `EGISZ_LICENSES` и `JPERSONS` загружаются в RAM на старт sync (словари/списки для O(1) поиска JID, OID, ИНН, наименований).

### ETL-логика (кратко)
* **Документ:** `relatesToMessage` в `MSGTEXT`; `localUid` / `DOCUMENTID` в `EGISZ_MESSAGES` и XML.
* **Клиника:** `gost-<jid>.infoclinica.lan` в `LOGTEXT`; `<organization>` → `EGISZ_LICENSES` / `JPERSONS` по JID.

### Mappings (витрина `fact_egisz_transactions` и вложенные view)

| Поле в DWH | Источник (FB / XML) | Смысл |
| :--- | :--- | :--- |
| `relates_to_id` | `<relatesToMessage>` | Ключ ответа ЕГИСЗ. |
| `local_uid_semd` | `<localUid>` / `DOCUMENTID` | Id СЭМД в МИС. |
| `jid` | gost / `MO_UID` / OID | Клиника. |
| `status` | `<status>` | `success` / `error` / `unknown`. |
| `errors_json` | `<errors>` | Коды и тексты РЭМД. |

### Дашборды Metabase (имена в UI)

Исходник — JSON в [`metabase_dashboards/`](../metabase_dashboards/); при старте пода импортируются `setup-dashboards.sh`. В UI имя дашборда = поле `name` в JSON (например **«01 Оперативный мониторинг»**). Ключевые объекты витрины: `v_egisz_transactions_enriched_ui`, `v_rpt_documents_no_response_ui`, `stg_parse_errors` (см. [`sql/001_schema.sql`](../sql/001_schema.sql)).

**`deploy` / `reset-deploy`** в `start.ps1` уже выполняют `DROP/CREATE` БД `metabase` и заново поднимают дашборды из образа. Точечно без полного деплоя: **`.\start.ps1 -Action build`**, затем **`.\start.ps1 -Action reset-metabase`**. **`apply`** — без сброса БД Metabase.

| Дашборд (`name` в JSON) | Содержание |
| :--- | :--- |
| **01 Оперативный мониторинг** | Статусы, топы СЭМД/клиник, лента; фильтры период / код СЭМД / JID. |
| **02 Сервис интеграции** | Поток по СЭМД и МО; период и срезы. |
| **03 Ошибки и разбор** | `stg_parse_errors` + error-витрина; периоды и код парсинга. |
| **04 Документы без ответа** | Очередь `v_rpt_documents_no_response_ui`; период «Отправлено». |
| **05 Тренды и динамика** | Объёмы и доля ошибок; период «День (тренд)». |
| **06 Качество данных** | JID/OID, полнота; период «Обработано». |
| **07 Глубокий анализ ошибок** | Тексты `errors_json`; период «Обработано». |
| **08 Агрегация ожидающих** | Очередь по клиникам, возрасту, СЭМД; период «Отправлено». |
| **09 Управленческий дашборд** | KPI, % ошибок, очередь, срезы по периоду. |

### Конфигурация (примеры)
* `config/egisz_corp.yaml` или переменные окружения.
* **Metabase (локальная отладка):** `admin@egisz.local` / `egisz` (секрет `metabase-admin`).

### Инфраструктура в K8s

| Сервис | В кластере | Примечание |
| :--- | :--- | :--- |
| **PostgreSQL** | `postgres.egisz-corp.svc.cluster.local:5432` | Витрина `egisz_reports` |
| **Metabase** | `metabase:3000` | С хоста: LB / `hostPort` 3000 / port-forward, см. `docs/METABASE.md` |
| **Config UI** | `conf-ui:8080` | Конфигурация ETL |
| **Airflow** | согласно `k8s/airflow/` | DAG ETL, опционально |

Полный обзор продукта: [корневой `README.md`](../README.md).
