# EGISZ Monitor Corp

Сервис мониторинга обмена между медицинскими информационными системами и федеральным контуром ЕГИСЗ / РЭМД: чтение журнала Firebird, разбор SOAP-ответов, загрузка витрины PostgreSQL и готовые дашборды Metabase.

## Содержание

- [Поток данных](#поток-данных)
- [Документация в репозитории](#документация-в-репозитории)
- [Стек](#стек)
- [Синхронизация Firebird → PostgreSQL](#синхронизация-firebird--postgresql)
- [Окно данных и справочники](#окно-данных-и-справочники)
- [Парсинг и обогащение](#парсинг-и-обогащение)
- [Основные поля витрины](#основные-поля-витрины)
- [Metabase и дашборды](#metabase-и-дашборды)
- [Healthcheck интеграции](#healthcheck-интеграции)
- [Конфигурация и доступы](#конфигурация-и-доступы)
- [Локальная инфраструктура](#локальная-инфраструктура)

## Поток данных

```text
Firebird: EXCHANGELOG, EGISZ_MESSAGES, EGISZ_LICENSES (+ JPERSONS)
  → парсинг MSGTEXT (SOAP/XML), сопоставление по MSGID, обогащение справочниками
  → PostgreSQL: fact_egisz_transactions, измерения, staging, отчётные представления
  → Metabase: JSON-дашборды из metabase_dashboards/*.json
```

## Документация в репозитории

| Документ | Содержание |
|----------|------------|
| [`AGENTS.md`](AGENTS.md) | Структура проекта, ориентиры для разработки |
| [`.cursorrules`](.cursorrules) | Домен: СЭМД, статусы, сигналы тревоги, интерпретация отчётов |
| [`docs/INTEGRATION_AUDIT.md`](docs/INTEGRATION_AUDIT.md) | Аудит сервиса: техника, бизнес, healthcheck |
| [`docs/METABASE.md`](docs/METABASE.md) | Провижининг Metabase, фильтры дат, обновление дашбордов |
| [`docs/KUBERNETES_LOCAL.md`](docs/KUBERNETES_LOCAL.md) | Локальный Kubernetes, сценарии `start.ps1` |
| [`docs/SYNC_DIAGNOSTICS.md`](docs/SYNC_DIAGNOSTICS.md) | Сверка объёмов Firebird / PostgreSQL, курсор ETL |

## Стек

| Слой | Используется |
|------|----------------|
| Язык | Python 3.10+ |
| Источник | Firebird; `firebird-driver`; клиент `fbclient` (`FB_CLIENT_LIBRARY`) |
| Витрина | PostgreSQL; `psycopg2-binary` |
| Конфигурация | YAML (`PyYAML`); `config/egisz_monitor.yaml` или `EGISZ_MONITOR_CONFIG` |
| Веб | Flask Config UI; ручной sync через `sync_routes` |
| Планировщик (опционально) | Apache Airflow, DAG `egisz_monitor_firebird_to_postgres` |
| Периодический ETL в k8s | CronJob `egisz-monitor-sync` — `egisz-monitor sync` каждые **15** мин (`k8s/etl-cron.yaml`) |
| Аналитика | Metabase; дашборды в `metabase_dashboards/*.json` |

Команды CLI в `pyproject.toml`: **`egisz-corp`** и **`egisz-monitor`** → модуль `egisz_monitor_corp.cli`.

## Синхронизация Firebird → PostgreSQL

Главная процедура — **`run_sync`** в [`egisz_monitor_corp/etl.py`](egisz_monitor_corp/etl.py). Firebird читается только **`SELECT`**-запросами; в PostgreSQL выполняются UPSERT и обновление staging.

### Курсоры и `etl_state`

| Поле / смысл | Поведение |
|----------------|-----------|
| **`last_log_id`** | Водяной знак по **`EXCHANGELOG.LOGID`** для пайплайна (по умолчанию `firebird_exchangelog`). |
| **`last_egmid`** | Обновляется **только после полного успешного** завершения прогона (журнал + исходящие). При сбое повторная выгрузка `EGISZ_MESSAGES` использует прежний курсор. |
| **`source_max_egmid`** | Пик последней выгрузки `EGISZ_MESSAGES` из Firebird; пишется сразу после выгрузки сообщений (для UI / диагностики). В Config UI поле EGMID показывает **max(`last_egmid`, `source_max_egmid`)** во время длинного прогона. |
| **`full_scan: true`** | Сбрасывает курсоры к началу; выборка журнала по-прежнему ограничена **`sync_window_days`**. |

### Порядок фаз `run_sync` (как в коде)

1. **Справочники** — полная выгрузка **`EGISZ_LICENSES`** с **`LEFT JOIN JPERSONS`**; отбор по **`MODIFYDATE`** в окне **`sync_window_days`** выполняется в Python после загрузки.
2. **Подсчёт журнала** — `COUNT` строк **`EXCHANGELOG`** с `LOGID > last_log_id` (для прогресса; ошибка подсчёта не останавливает sync).
3. **`EGISZ_MESSAGES`** — подсчёт строк в окне (фаза прогресса `messages_counting`), затем постраничная выгрузка по **`EGMID`** + **`CREATEDATE`** в пределах **`sync_window_days`**. В **`etl_state`** сразу обновляется **`source_max_egmid`**.
4. **`EXCHANGELOG`** — страницы `FIRST {batch_size}` с `LOGID > last_id`, сортировка по **`LOGID`**. Строки сопоставляются с выгруженными сообщениями по **`MSGID`** в памяти; **`MSGTEXT`** разбирается как SOAP/XML; после каждой страницы двигается **`last_log_id`** до максимального обработанного **`LOGID`**.
5. **Исходящие документы** — полная перезапись **`stg_egisz_outbound_documents`** из Firebird для отчёта «Документы без ответа» (после успешной обработки журнала).

Ошибки разбора без построения факта пишутся в **`stg_parse_errors`**. На старте в PostgreSQL идемпотентно применяются **`001_schema.sql`**, **`002_etl_state.sql`**, **`005_healthcheck.sql`**.

**Один запуск на пайплайн:** `pg_try_advisory_lock(hash(pipeline_name))` — параллельно не выполняются ручной sync из UI и CronJob; при занятости lock второй процесс получает **`PipelineLockBusyError`** (CLI / CronJob — код **75**).

### Как запустить

| Способ | Описание |
|--------|----------|
| `egisz-monitor sync` | CLI; `--config` или `EGISZ_MONITOR_CONFIG` |
| Config UI | `run_sync` в фоне; повторный старт при активном sync отклоняется |
| Apache Airflow | DAG проверяет соединения и вызывает `run_sync` |
| `kubectl exec deploy/conf-ui -- egisz-monitor sync` | Ручной запуск в поде **conf-ui** |
| CronJob **`egisz-monitor-sync`** | Тот же образ и Secret, что у Deployment; расписание ***/15** в UTC |

`start.ps1` поднимает кластер и схему; **полный** прогон ETL в `deploy` / `apply` по умолчанию **не** встроен (ETL — по кнопке, CLI или CronJob).

## Окно данных и справочники

Окно журнала ограничивается по **`LOGDATE`** (в SQL: смещение от текущего момента на **`sync_window_days`** суток). Пакетная обработка журнала — страницы размера **`batch_size`**.

Перед циклом журнала выполняется выгрузка лицензий (см. выше); инкрементальная выгрузка **`EGISZ_MESSAGES`** фильтруется в SQL по **`CREATEDATE`** (и курсору **`EGMID`**).

## Парсинг и обогащение

Факт в **`fact_egisz_transactions`** строится, если из SOAP-ответа восстанавливается связь с исходящим запросом:

- **`relates_to_id`** — из `<relatesToMessage>` в XML в **`EXCHANGELOG.MSGTEXT`**. Без связи строка не попадает в факт → запись в **`stg_parse_errors`**.
- **`local_uid_semd`** — `<localUid>` в XML, иначе **`EGISZ_MESSAGES.DOCUMENTID`**.
- **`status`** — нормализация в `success` / `error` / `unknown`.
- **`errors_json`** — массив `<errors>` из ответа РЭМД без переписывания текста.

**Клиника (JID):**

1. Токен `gost-<jid>.infoclinica.lan` в **`MSGTEXT`**, затем **`LOGTEXT`**, затем **`EGISZ_MESSAGES.REPLYTO`**.
2. Сопоставление **`REPLYTO`** с **`MO_DOMEN`** в предзагруженных строках лицензий (как в Firebird: вхождение домена, выбор по **`MODIFYDATE`**).
3. **`MO_UID`** из XML или лицензий → карта **`MO_UID → JID`**.
4. Наименование, ИНН, **`FIR_OID`** — из **`JPERSONS`** и **`EGISZ_LICENSES`**.

Парсер: модуль [`egisz_monitor_corp/parser.py`](egisz_monitor_corp/parser.py) (`EgiszMonitorParser`).

## Основные поля витрины

| Поле | Источник | Смысл |
|------|----------|--------|
| `relates_to_id` | `<relatesToMessage>` в `MSGTEXT` | Связь ответа РЭМД с исходящим запросом |
| `local_uid_semd` | `<localUid>` или `DOCUMENTID` | Идентификатор экземпляра СЭМД; поиск «без ответа» |
| `jid` | gost-токены, лицензии, `MO_UID` | Внутренний идентификатор клиники |
| `kind_code` | `<kind>` в XML или `EGISZ_LICENSES.KIND` | Тип СЭМД; в `*_ui` — текст для Metabase |
| `status` | `<status>` в XML | `success` / `error` / `unknown` |
| `errors_json` | `<errors>` в XML | Сырые коды и тексты отказов |
| `errors_friendly` / «Сводка ошибок» | `egisz_friendly_error_item`, `egisz_friendly_errors_row` | Человекочитаемая сводка для отчётов |

Полная схема: [`sql/001_schema.sql`](sql/001_schema.sql).

## Metabase и дашборды

JSON лежат в [`metabase_dashboards/`](metabase_dashboards/); при старте пода Metabase [`metabase/provision.sh`](metabase/provision.sh) вызывает `setup-dashboards.sh` и создаёт дашборды в **корне личной коллекции** администратора. Большинство карточек используют **`v_egisz_transactions_enriched_ui`** и **`v_rpt_documents_no_response_ui`**. Для рейтингов ошибок на дашбордах **07**, **09**, **10** берётся первый значимый элемент массива «Ошибки JSON» на транзакцию, чтобы один документ не дублировался в топах. Дашборд **11** — представления **`v_health_*_ui`** ([`sql/005_healthcheck.sql`](sql/005_healthcheck.sql)).

### Каталог дашбордов (01–11)

| Файл | Название в Metabase | Содержание и типовые карточки |
|------|---------------------|-------------------------------|
| `01_operational.json` | 01 Оперативный мониторинг | Field filters: период (**`dwh_date`**), код СЭМД, JID; таблица последних операций (статусы, СЭМД, клиника, РЭМД, VPN-хост, `localUid`, ошибки JSON). |
| `02_service.json` | 02 Сервис интеграции | Поток по типам СЭМД и клиникам: топ типов СЭМД, объём по клиникам (bar / таблицы). |
| `03_errors.json` | 03 Ошибки и разбор | **`stg_parse_errors`** (код ошибки парсинга) + деталь отказов РЭМД по витрине; разные периоды для staging и фактов. |
| `04_documents_no_response.json` | 04 Документы без ответа | Очередь исходящих без callback с тем же **`localUid`**; период по дате «Отправлено». |
| `05_trends.json` | 05 Тренды и динамика | Транзакции по дням и статусам; динамика по типам СЭМД; доля ошибок по дням; объём по часам (72 ч). |
| `06_quality.json` | 06 Качество данных | Успешность по клиникам и типам СЭМД; целостность JID/OID; полнота полей витрины. |
| `07_errors_deep.json` | 07 Глубокий анализ ошибок | Топ формулировок отказов ЕГИСЗ (`egisz_friendly_error_item`); ошибки по клиникам и СЭМД. |
| `08_pending_agg.json` | 08 Агрегация ожидающих | Топ клиник с «зависшими» документами; возраст очереди; разрезы по типам СЭМД; детализация. |
| `09_executive.json` | 09 Управленческий дашборд | KPI, доли статусов, топ причин ошибок, объёмы по СЭМД и клиникам, очередь (кол-во, срок, тип СЭМД, топ клиник), рейтинги ошибок. |
| `10_errors_top.json` | 10 Топы ошибок | Итоги за период; pie по ЕГИСЗ / СЭМД / клиникам; рейтинги формулировок, СЭМД, клиник; сводная таблица «тип ошибки × СЭМД × клиника». |
| `11_healthcheck.json` | 11 Healthcheck интеграции | Сигналы **`v_health_signals`**, топ клиник по error rate (24ч), heatmap клиника × дни, возраст очереди, тренд **`stg_parse_errors`**, сводка прокси-БД. |

Имена карточек на дашбордах **02–11** в JSON с префиксом **`NN ·`** для уникальности в коллекции (см. [`metabase_dashboards/README.md`](metabase_dashboards/README.md)).

### Обновление дашбордов и образа Metabase

- **`deploy`** / **`reset-deploy`** — пересоздание БД приложения Metabase (`metabase`) и повторный провижининг.
- **`apply`** — манифесты и перезапуск сервисов; БД Metabase сохраняется.
- Изменили только JSON — нужны **`.\start.ps1 -Action build`** и перезапуск Metabase. При **`METABASE_FORCE_PROVISION=auto`** провижининг **пропускается**, если все **11** EGISZ-дашбордов уже есть; для принудительной перезаливки: **`.\start.ps1 -Action reset-metabase`**. Тег образа **`:k8s-v15`** задан в [`k8s/metabase.yaml`](k8s/metabase.yaml) и `start.ps1` — **bump** при следующем изменении JSON или скриптов.
- Обновление только схемы витрины или данных ETL образ Metabase **не** требует.

## Healthcheck интеграции

Три связанных слоя: SQL ([`sql/005_healthcheck.sql`](sql/005_healthcheck.sql) → `v_health_by_clinic`, `v_health_signals`, `v_health_proxy_db`), API **`GET /api/healthcheck`** в Config UI (таймаут **10 s**), дашборд **11** и боковая панель Healthcheck в UI (опрос **30 s**).

Сигналы по умолчанию (детали и триаж — [`docs/INTEGRATION_AUDIT.md`](docs/INTEGRATION_AUDIT.md)):

| Сигнал | Условие | Уровень |
|--------|---------|---------|
| `error_rate_high` | error-rate за 24 ч > 10% при объёме ≥ 50 | red |
| `unknown_high` | unknown за 24 ч > 5% при объёме ≥ 20 | yellow |
| `parse_errors_burst` | parse_errors за 1 ч > 10 | red |
| `queue_red_24h` | в очереди > 24 ч более 50 документов | red |
| `cursor_stale` | `etl_state.updated_at` старше 6 ч | red |

## Конфигурация и доступы

Параметры: [`config/egisz_monitor.yaml`](config/egisz_monitor.example.yaml) (шаблон), в Kubernetes — [`k8s/local/egisz_monitor.yaml`](k8s/local/egisz_monitor.yaml).

| Компонент | Типовые значения (локально) |
|-----------|-----------------------------|
| Firebird | С пода k8s на Windows: **`host.docker.internal:3050`**; алиас/БД из конфига; часто **`SYSDBA`** / **`masterkey`**; **`WIN1251`** |
| PostgreSQL | **`postgres.egisz-monitor.svc.cluster.local:5432`**; БД **`egisz_reports`**; пользователь **`egisz`** / пароль из секрета |
| Metabase | Админ **`admin@egisz.local`** / **`egisz`**; UI **`http://127.0.0.1:3000`** (совпадайте с **`MB_SITE_URL`** и браузером) |

## Локальная инфраструктура

Namespace: **`egisz-monitor`**. Быстрый старт с хоста Windows:

```powershell
.\start.ps1 -Action deploy
```

| Сервис | Доступ | Назначение |
|--------|--------|------------|
| PostgreSQL | `postgres:5432` в кластере; с хоста при необходимости NodePort **30432** | Витрина **`egisz_reports`**, БД приложения Metabase |
| Metabase | Service **`metabase`**, порт **3000**; на Docker Desktop **LoadBalancer** → часто **`http://127.0.0.1:3000`** | Дашборды |
| Config UI | Service **`conf-ui`**, порт **8080**; LoadBalancer → часто **`http://127.0.0.1:8080`** | Конфиг, sync, healthcheck API |
| Airflow | Helm / `k8s/airflow/` | Опциональный планировщик |

Если **LoadBalancer** в состоянии Pending (например **kind**), используйте **`.\start.ps1 -Action web`** (port-forward) или см. [`docs/KUBERNETES_LOCAL.md`](docs/KUBERNETES_LOCAL.md). Полный список действий: **`.\start.ps1 -Action help`**.
