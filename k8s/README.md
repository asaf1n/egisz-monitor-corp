# Kubernetes (`k8s/`)

Каталог содержит манифесты для namespace **`egisz-corp`**: PostgreSQL (витрина), Metabase (аналитика + провижининг дашбордов), Config UI, Job’ы схемы SQL. Типовой сценарий на Docker Desktop: **`.\start.ps1 -Action deploy`** (сборка образов, `kubectl apply`, ожидание rollout, `verify`) или **`apply`** / **`reset-deploy`**. Подробности портов, Metabase, кириллицы: [`docs/METABASE.md`](../docs/METABASE.md).

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

Исходник — JSON в [`metabase_dashboards/`](../metabase_dashboards/); при старте пода импортируются `setup-dashboards.sh`. Ключевые объекты витрины: `v_egisz_transactions_enriched_ui`, `v_rpt_documents_no_response_ui`, `stg_parse_errors` (см. [`sql/001_schema.sql`](../sql/001_schema.sql)).

| Дашборд | Содержание (по `name` в JSON) |
| :--- | :--- |
| **01** | Оперативный мониторинг — сводка за 24 ч, топы СЭМД/клиник, лента последних операций. |
| **02** | Сервис интеграции — статистика по СЭМД и клиникам. |
| **03** | Ошибки и разбор — `stg_parse_errors` и error-строки витрины. |
| **04** | Документы без ответа — очередь `v_rpt_documents_no_response_ui`. |
| **05** | Тренды и динамика — объёмы и доля успеха во времени. |
| **06** | Качество данных — успешность, контроль JID/OID. |
| **07** | Глубокий анализ ошибок — агрегация текстов/кодов из `errors_json`. |
| **08** | Агрегация ожидающих — зависшие документы (клиники, срок ожидания, типы СЭМД). |
| **09** | Управленческий дашборд — KPI, % ошибок, длина очереди, срезы. |

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
