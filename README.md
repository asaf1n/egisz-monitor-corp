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
- [Ручная диагностика синка](#ручная-диагностика-синка)
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
| [`AGENTS.md`](AGENTS.md) | Структура кода, Metabase/k8s для агентов, стиль документации |
| [`.cursorrules`](.cursorrules) | Домен: СЭМД, статусы, сигналы тревоги, интерпретация отчётов |
| [`docs/BI_EGISZ_INFOKLINIKA_AUDIT.md`](docs/BI_EGISZ_INFOKLINIKA_AUDIT.md) | Полный аудит BI и интеграции (техника, витрина, Metabase §4, healthcheck, k8s §8, роли) |

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

При **`.\start.ps1`** с действиями **`deploy`**, **`apply`**, **`start`**, **`reset-deploy`** после готовности Postgres пересобирается ConfigMap **`egisz-reports-schema`** из файлов в репозитории и запускается Job **`egisz-reports-schema-init`**, который по **[`sql/schema_apply_order.txt`](sql/schema_apply_order.txt)** применяет весь DDL витрины в **`egisz_reports`** (идемпотентно, **без** удаления баз PostgreSQL). Так появляются новые объекты схемы (в том числе **`stg_jpersons_import`**), без отдельного ручного прогона, если кластер обновляется через `start.ps1`.

### Курсоры и `etl_state`

**Термины:** **`MSGID`** — идентификатор сообщения в контуре обмена и интеграций; в **`EXCHANGELOG`** на него ссылается колонка **`MSGID`**, тело SOAP-колбэка (если есть) — в **`EXCHANGELOG.MSGTEXT`**. **`EGMID`** — суррогатный ключ **строки** в таблице **`EGISZ_MESSAGES`** (фиксация исходящего при отправке документа в РЭМД ЕГИСЗ).

| Поле / смысл | Поведение |
|----------------|-----------|
| **`last_log_id`** | Водяной знак по **`EXCHANGELOG.LOGID`** для пайплайна (по умолчанию `firebird_exchangelog`). |
| **`last_egmid`** | Ватермарк по **max(`EGISZ_MESSAGES.EGMID`)** среди строк **журнала `EXCHANGELOG`**, обработанных в успешном прогоне. Обновляется **только после полного успешного** завершения прогона (журнал + исходящие). При сбое не двигается. |
| **`messages_snapshot_high_egmid`** | Верхняя граница **инкрементальной** выгрузки снимка **`EGISZ_MESSAGES`** в **`stg_egisz_messages_journal`**: в Firebird читаются строки с **`EGMID >`** этого значения (тот же отбор **непустой `DOCUMENTID`**, окно **`CREATEDATE`** при **`sync_window_days` > 0**). После успешного sync обновляется до max **EGMID** среди строк, прочитанных упорядоченными страницами в этом прогоне. Догрузка по **MSGID** из пакетов журнала по-прежнему делает **UPSERT** в staging. **Полный пересъём** staging и сброс курсора в **0**: **`etl.sync_window_days <= 0`** (нет окна по дате в Firebird + **TRUNCATE** перед проходом). |
| **`source_max_egmid`** | Пик **`EGMID`**, записанный в конце прогона по результатам журнала (колонка **`source_max_egmid`** в **`etl_state`**). В карточке healthcheck «Прокси-БД» и связанном UI для сравнения со staging используется **GREATEST(`last_egmid`, `source_max_egmid`)** (поле выборки в SQL по-прежнему может называться `etl_cursor_egmid` в представлении). |
| **`full_scan`** | **Не используется** текущим кодом (поле отсутствует в `EtlConfig`). |

### Порядок фаз `run_sync` (как в коде)

1. **Справочники** — сразу после advisory lock: полные выборки **`JPERSONS`** и **`EGISZ_LICENSES`** из Firebird (без JOIN в Firebird). В PostgreSQL: **`stg_jpersons_import`**, **`stg_egisz_licenses_import`**, сшивка **`JNAME` / `JINN` / `FIR_OID`** через **`UPDATE … FROM`**, затем **`merge_dim_clinics_from_license_staging`**. В режиме **`dry_run`** без записи в PG сшивка выполняется в Python.
2. **Готовность к журналу** — фаза прогресса `counting` / `exchangelog_ready` (в payload передаётся **`etl_last_egmid`** = текущее **`etl_state.last_egmid`**, это ватермарк журнала, а не позиция выгрузки снимка сообщений); **`COUNT` по журналу в Firebird для UI не выполняется** (заглушка объёма; в payload не передаётся «фиктивный» ноль как знаменатель).
3. **Журнал и снимок сообщений** — чередование страниц: **`EXCHANGELOG`** по **`LOGID`** (при **`sync_window_days` > 0** — дополнительно окно по **`LOGDATE`**; при **`sync_window_days` <= 0** — без фильтра по дате, все строки за курсором **`LOGID`**) и выгрузка **`EGISZ_MESSAGES`** в **`stg_egisz_messages_journal`** (при **`sync_window_days` > 0** — инкремент по **`etl_state.messages_snapshot_high_egmid`**, **`EGMID >`** курсора и prune по окну; при **`sync_window_days` <= 0** — **TRUNCATE** staging, сброс курсора, без окна по **`CREATEDATE`**). При разборе пакета недостающие **`MSGID`** догружаются из Firebird. Парсинг **`MSGTEXT`**, UPSERT в **`fact_egisz_transactions`**.
4. **Исходящие для отчёта «без ответа»** — один запрос к Firebird: **`EGISZ_MESSAGES`** с непустым **`DOCUMENTID`**; при **`sync_window_days` > 0** — только строки в окне **`CREATEDATE`** (как **`LOGDATE`** у журнала); при **`sync_window_days` <= 0** — все такие строки без ограничения по дате. В PostgreSQL таблица **`stg_egisz_outbound_documents`** каждый успешный sync **полностью очищается и заполняется заново**.

Факты в **`fact_egisz_transactions`** пишутся **чанками** (`facts_upsert_chunk_size` в YAML `etl`, при необходимости **`pg_upsert_statement_timeout_sec`** — см. [`config/egisz_monitor.example.yaml`](config/egisz_monitor.example.yaml)). Ошибки разбора без факта — в **`stg_parse_errors`**. На старте ETL и в k8s Job идемпотентно применяется полный набор DDL из **[`sql/schema_apply_order.txt`](sql/schema_apply_order.txt)** (по умолчанию **`001_schema.sql`**, **`002_etl_state.sql`**, **`005_healthcheck.sql`**; без DROP баз PostgreSQL).

**Один запуск на пайплайн:** `pg_try_advisory_lock(hash(pipeline_name))` — параллельно не выполняются ручной sync из UI и CronJob; при занятости lock второй процесс получает **`PipelineLockBusyError`** (CLI / CronJob — код **75**).

### Config UI: откуда берутся строка состояния, лог и «Последние значения»

| Блок в UI | Источник | Зачем смотреть |
|-----------|----------|----------------|
| **Строка над формой** | `GET /api/sync/status`: фаза **`progress.phase`**, последняя строка **`message`** (`progress_cb`), числовые поля **`progress`** | Текущий шаг и факты прогона; **процент «от всего объёма»** показывается только если известен знаменатель (например исходящие с `outbound_total`). Иначе — неопределённая полоска и **«…»** — это ожидаемо (без тяжёлого `COUNT` в Firebird). |
| **System log** | То же + блок «Курсор прогона (payload ETL)» и строки **last_log_id** / **last_egmid** / дата лицензий | Сводка для копирования; при **running** включается текст лога ETL. |
| **Последние значения синхронизации** | **`GET /api/pg/sync-snapshot`** → таблица **`etl_state`** | После **завершения** синка — зафиксированные курсоры. **Во время синка** значения могут **подменяться** полями из payload текущего прогона (**`etl_last_egmid`** = текущее **`last_egmid`** из **`etl_state`**, ватермарк журнала; см. подсказку под заголовком в UI). |

### Как запустить синхронизацию

| Способ | Описание |
|--------|----------|
| `egisz-monitor sync` | CLI; `--config` или `EGISZ_MONITOR_CONFIG` |
| Config UI | `run_sync` в фоне; повторный старт при активном sync отклоняется |
| Apache Airflow | DAG проверяет соединения и вызывает `run_sync` |
| `kubectl exec deploy/conf-ui -- egisz-monitor sync` | Ручной запуск в поде **conf-ui** |
| CronJob **`egisz-monitor-sync`** | Тот же образ и Secret, что у Deployment; расписание ***/15** в UTC |

`start.ps1` поднимает кластер; **полный** прогон ETL в `deploy` / `apply` по умолчанию **не** встроен (ETL — по кнопке, CLI или CronJob). DDL витрины при этом на каждом таком запуске `start.ps1` применяется автоматически (см. абзац выше).

## Окно данных и справочники

### Выборка из Firebird (один прогон `run_sync`)

| Источник | Как читается | Предикат / курсор | Назначение в PostgreSQL |
|----------|----------------|-------------------|-------------------------|
| **`JPERSONS`** | Полная выборка | — | **`stg_jpersons_import`** → обогащение лицензий и **`dim_clinics`** |
| **`EGISZ_LICENSES`** | Полная выборка | — | **`stg_egisz_licenses_import`** → **`dim_clinics`**, **`KIND`** для типа СЭМД при отсутствии в XML |
| **`EXCHANGELOG`** | Постранично (`batch_size`, max 65k на страницу) | **`LOGID > last_log_id`**; при **`sync_window_days > 0`** также **`LOGDATE`** за последние N суток | Только колонки журнала (**без JOIN** к сообщениям в Firebird) |
| **`EGISZ_MESSAGES`** (снимок для журнала) | Постранично в PostgreSQL **`stg_egisz_messages_journal`** (в одном прогоне чередуется с пакетами **`EXCHANGELOG`**, затем дочитывается до конца того же отбора) | Тот же горизонт **`CREATEDATE`**, что у исходящих (`sync_window_days`); **непустой `DOCUMENTID`** (как у outbound); порядок выгрузки из Firebird по **`EGMID`/`MSGID`** | Сопоставление с журналом в PG: **`EXCHANGELOG.MSGID` = `EGISZ_MESSAGES.MSGID`**; **`last_egmid`** в **`etl_state`** на пагинацию этого снимка **не** влияет |
| **`EGISZ_MESSAGES`** (staging очереди) | Один SELECT после журнала | Непустой **`DOCUMENTID`**; при **`sync_window_days > 0`** — **`CREATEDATE`** за N суток; **`ORDER BY EGMID DESC`** (в ETL остаётся одна строка на **`DOCUMENTID`**) | Полная перезапись **`stg_egisz_outbound_documents`** |

### Парсинг и обогащение

| Элемент данных | Где берётся | Как используется |
|----------------|-------------|-------------------|
| Тело SOAP/XML ответа РЭМД | **`EXCHANGELOG.MSGTEXT`** | Разбор статуса, **`relatesToMessage`**, **`localUid`**, **`kind`**, **`errors`**, даты регистрации и т.д. → **`fact_egisz_transactions`** |
| Транспортный URL | **`EXCHANGELOG.LOGTEXT`** | Извлечение **`gost-…`** / JID вместе с **`REPLYTO`** |
| Исходящее сообщение (метаданные) | **`EGISZ_MESSAGES`** по **`MSGID`** из журнала (через **`stg_egisz_messages_journal`** в PostgreSQL) | **`DOCUMENTID`**, **`REPLYTO`**, **`CREATEDATE`**, **`EGMID`** → связь с колбэком, **`processed_at`**, лицензии по домену |
| Тип СЭМД | XML **`<kind>`**; fallback **`EGISZ_LICENSES.KIND`** | **`kind_code`**, **`dim_semd_types`** |
| Клиника | **`gost`** в LOGTEXT/REPLYTO, **`EGISZ_LICENSES`**, **`MO_UID`**, **`JPERSONS`** | **`jid`**, **`clinic_name`**, флаги расхождения источников |
| Ошибка разбора / нет связи | — | **`stg_parse_errors`**, без строки в **`fact_egisz_transactions`** |

Факт в **`fact_egisz_transactions`** строится, если из SOAP-ответа восстанавливается связь с исходящим запросом:

- **`relates_to_id`** — из `<relatesToMessage>` в XML в **`EXCHANGELOG.MSGTEXT`**. Без связи строка не попадает в факт → запись в **`stg_parse_errors`**.
- **`local_uid_semd`** — `<localUid>` в XML, иначе **`EGISZ_MESSAGES.DOCUMENTID`**.
- **`status`** — нормализация в `success` / `error` / `unknown`.
- **`errors_json`** — массив `<errors>` из ответа РЭМД без переписывания текста.

**Клиника (JID):**

Идентификатор клиники собирается из транспортных полей журнала и справочников Firebird:

1. В URL ищется хост вида **`gost-<…>.infoclinica.lan`** в **`EXCHANGELOG.LOGTEXT`** и в **`EGISZ_MESSAGES.REPLYTO`**. Если в обоих полях есть числовой JID в пути, используется значение из **LOGTEXT**, иначе из **REPLYTO** (как в [`egisz_monitor_corp/parser.py`](egisz_monitor_corp/parser.py)).
2. **`EGISZ_MESSAGES.REPLYTO`** сопоставляется с **`EGISZ_LICENSES.MO_DOMEN`** среди предзагруженных лицензий (вхождение домена, выбор строки по **`MODIFYDATE`**).
3. Из выбранной лицензии берётся **`EGISZ_LICENSES.JID`**; при разрешении по организации используются **`MO_UID`** из XML ответа РЭМД или из лицензий и карта **`MO_UID → JID`**.
4. Наименование медорганизации, ИНН и **`FIR_OID`** подставляются из **`JPERSONS`** и **`EGISZ_LICENSES`**.

Парсер: модуль [`egisz_monitor_corp/parser.py`](egisz_monitor_corp/parser.py) (`EgiszMonitorParser`).

## Основные поля витрины

| Поле | Источник | Смысл |
|------|----------|--------|
| `relates_to_id` | `<relatesToMessage>` в `MSGTEXT` | Связь ответа РЭМД с исходящим запросом |
| `exchangelog_log_id` | `EXCHANGELOG.LOGID` в выгрузке журнала | Водяной знак строки журнала на факте (удобно для SQL без join к сырью) |
| `egisz_messages_egmid` | `EGISZ_MESSAGES.EGMID` из **`stg_egisz_messages_journal`** (по **`EXCHANGELOG.MSGID` = `MSGID`**) | Суррогатный ключ **строки** сообщения в прокси-БД (фиксация исходящего при отправке в РЭМД); рядом с **`LOGID`** на факте |
| `local_uid_semd` | `<localUid>` или `DOCUMENTID` | Идентификатор экземпляра СЭМД; поиск «без ответа» |
| `jid` | `EXCHANGELOG.LOGTEXT`, `EGISZ_MESSAGES.REPLYTO`, `EGISZ_LICENSES.MO_DOMEN` / `JID`, `MO_UID` | Идентификатор клиники в контуре интеграции |
| `kind_code` | `<kind>` в XML или `EGISZ_LICENSES.KIND` | Тип СЭМД; в `*_ui` — текст для Metabase |
| `status` | `<status>` в XML | `success` / `error` / `unknown` |
| `errors_json` | `<errors>` в XML | Сырые коды и тексты отказов |
| `errors_friendly` / «Сводка ошибок» | `egisz_friendly_error_item`, `egisz_friendly_errors_row` | Человекочитаемая сводка для отчётов |

Полная схема: [`sql/001_schema.sql`](sql/001_schema.sql).

## Metabase и дашборды

Описания отчётов задаются JSON в [`metabase_dashboards/`](metabase_dashboards/); при старте пода Metabase [`metabase/provision.sh`](metabase/provision.sh) вызывает `setup-dashboards.sh` и создаёт дашборды в **корне личной коллекции** администратора (см. [`docs/BI_EGISZ_INFOKLINIKA_AUDIT.md`](docs/BI_EGISZ_INFOKLINIKA_AUDIT.md) §4). По витрине колбэков агрегаты считают **документ**, а не строку журнала: **`COUNT(DISTINCT "Связанное сообщение")`** соответствует одному `relates_to_id` на колбэк. Для **очереди без ответа** в отчётах используется **`COUNT(DISTINCT "localUid СЭМД")`** (один исходящий документ). На дашборде **04** топы по тексту отказа РЭМД строятся по **первому значимому** элементу JSON «Ошибки JSON» **на документ**, чтобы один отказ не размножал строки в рейтинге; на **05** та же логика для управленческих карточек по ошибкам.

Блок **healthcheck** (сигналы, heatmap, очередь, парсинг, прокси-БД) на дашборде **02** читает представления **`v_health_*_ui`** из [`sql/005_healthcheck.sql`](sql/005_healthcheck.sql); в Config UI те же данные доступны через **`GET /api/healthcheck`**. Ошибки **разбора канала** (битый XML, нет `relatesToMessage`) — внизу **02** (`v_stg_parse_errors_by_document`, фильтр **`parse_created_filter`**) и в сигнале **parse_errors_burst**.

### Каталог дашбордов (пять JSON)

**`01_operational.json` — «01 Оперативный мониторинг и динамика».** Срез для смены и L2: последние операции, статусы, ошибки по СЭМД и клиникам, топы, «% ошибок»; плюс **тренды** (календарь по «День (тренд)», объём по часам за 72 ч). Фильтры URL: `dwh_date_filter`, `top_semd_filter`, `top_clinic_filter`.

**`02_service.json` — «02 Сервис, healthcheck и парсинг журнала».** Нагрузка по витрине (топы СЭМД и клиник), healthcheck (сигналы, heatmap, очередь, прокси-БД), почасовой тренд парсинга и **две детальные карточки** staging с отдельным фильтром даты **`parse_created_filter`** и **`err_parse_code_filter`**.

**`03_documents_no_response.json` — «03 Документы без ответа».** Очередь callback: список, топы, возраст ожидания, типы СЭМД, детализация. Дата по **«Отправлено»**; те же **код СЭМД** и **JID**, что на **01**.

**`04_quality_and_errors.json` — «04 Ошибки и качество данных».** Качество витрины (успешность, JID/OID, полнота полей) и разбор отказов РЭМД (топы, доли, сводные таблицы). Общий период по **«Обработано IPS»**.

**`05_executive.json` — «05 Управление СЭМД».** Сводки для руководства (витрина + очередь, разные привязки `dwh_date` на карточках — см. [`docs/BI_EGISZ_INFOKLINIKA_AUDIT.md`](docs/BI_EGISZ_INFOKLINIKA_AUDIT.md) §4.9).

**`06_semd_archive.json` — «06 Архив СЭМД».** Итоги по фильтру (документы, клиники), столбчатая диаграмма «тип СЭМД → число документов» и **полная таблица** `v_rpt_semd_archive_ui` с фильтрами по дате обработки, СЭМД и JID.

Имена сохранённых вопросов (карточек) в JSON: на **01** часть карточек без префикса (оперативный блок), остальные и дашборды **02–06** — с префиксом **`NN ·`**. Таблица «файл → имя в UI» — в [`AGENTS.md`](AGENTS.md) (раздел Metabase).

### Обновление дашбордов и образа Metabase

- **`deploy`** / **`reset-deploy`** — пересоздание БД приложения Metabase (`metabase`) и повторный провижининг.
- **`apply`** — манифесты и перезапуск сервисов; БД Metabase сохраняется.
- Изменили только JSON — **`.\start.ps1 -Action restart-metabase`** (или **`deploy`** / **`reset-deploy`** для сброса БД приложения). При **`METABASE_FORCE_PROVISION=auto`** провижининг **пропускается**, если все EGISZ-дашборды из каталога `metabase_dashboards/` уже есть; для принудительной перезаливки без ручного SQL: **`.\start.ps1 -Action deploy`** / **`reset-deploy`** или временно **`METABASE_FORCE_PROVISION=true`** в [`k8s/metabase.yaml`](k8s/metabase.yaml). Тег образа **`:k8s-v23`** задан в [`k8s/metabase.yaml`](k8s/metabase.yaml) и `start.ps1` — **bump** при следующем изменении JSON или скриптов.
- Обновление только схемы витрины или данных ETL образ Metabase **не** требует.

### Выгрузка JSON из Metabase (обратно в формат репозитория)

- **Config UI** (боковая панель): кнопка **«Выгрузка Metabase → JSON»** над **Backup DWH** — скачивается ZIP с JSON дашбордов из **личной коллекции** администратора Metabase (тот же формат файлов, что в каталоге **`metabase_dashboards/`** для `setup-dashboards.sh`). Под кнопкой указана папка в репозитории: **`metabase_dashboards/`**. В кластере conf-ui ходит к Metabase по внутреннему URL и учётке из Secret **`metabase-admin`** (см. [`k8s/conf-ui.yaml`](k8s/conf-ui.yaml)).
- **CLI в образе Metabase** (отладка в поде): `PYTHONPATH=/app METABASE_URL=http://localhost:3000 python3 -m egisz_monitor_corp.metabase_export` — запись в **`METABASE_EXPORT_DIR`** или в **`/app/metabase_dashboards`** (если задан каталог в образе). С хоста проще: **`py -3 metabase/export_dashboards_from_api.py`** при port-forward на порт 3000 и переменных **`METABASE_URL`**, **`METABASE_ADMIN_*`**.

## Healthcheck интеграции

Три связанных слоя: SQL ([`sql/005_healthcheck.sql`](sql/005_healthcheck.sql) → `v_health_by_clinic`, `v_health_signals`, `v_health_proxy_db`), API **`GET /api/healthcheck`** в Config UI (таймаут **10 s**), дашборд **02** (блок healthcheck) и боковая панель Healthcheck в UI (опрос **30 s**).

Сигналы по умолчанию (детали и триаж — [`docs/BI_EGISZ_INFOKLINIKA_AUDIT.md`](docs/BI_EGISZ_INFOKLINIKA_AUDIT.md) §3):

| Сигнал | Условие | Уровень |
|--------|---------|---------|
| `error_rate_high` | error-rate за 24 ч > 10% при объёме ≥ 50 | red |
| `unknown_high` | unknown за 24 ч > 5% при объёме ≥ 20 | yellow |
| `parse_errors_burst` | уникальных документов с ошибкой парсинга за 1 ч > 10 (`v_stg_parse_errors_by_document`) | red |
| `queue_red_24h` | в очереди > 24 ч более 50 документов | red |
| `cursor_stale` | `etl_state.updated_at` старше 6 ч | red |

## Ручная диагностика синка

Сверка **окна** и **курсора** с кодом ETL: предикаты по **`LOGDATE`** / **`CREATEDATE`** и пагинация по **`LOGID`** заданы в Python-модуле [`egisz_monitor_corp/sql_util.py`](egisz_monitor_corp/sql_util.py) (`exchangelog_inner_sql_for_etl`, `paginated_exchangelog_sql`, `journal_messages_staging_base_sql`, `outbound_documents_staging_select`). Для **COUNT** во Firebird соберите запрос вручную в DBeaver по тем же фрагментам SQL, что возвращают эти функции (в репозитории **нет** отдельного файла с шаблонами).

**Параметры ETL:** `etl.sync_window_days`, `etl.batch_size`, `etl.pipeline_name` — в [`config/egisz_monitor.yaml`](config/egisz_monitor.yaml) (или ConfigMap / UI); имя пайплайна по умолчанию **`firebird_exchangelog`**. Значение **`sync_window_days: 0`** (или отрицательное) — **без фильтра по дате в Firebird** для **`EXCHANGELOG`**, **`EGISZ_MESSAGES`** (снимок и исходящие) и одновременно **полный пересъём** снимка в **`stg_egisz_messages_journal`** (**TRUNCATE** + сброс **`messages_snapshot_high_egmid`**).

**Курсор в PostgreSQL** (витрина `egisz_reports`):

```sql
SELECT pipeline, last_log_id, last_egmid, messages_snapshot_high_egmid, source_max_egmid, source_max_licenses_modifydate, updated_at
FROM etl_state
WHERE pipeline = 'firebird_exchangelog';
```

**Ориентиры в PostgreSQL** (после синка):

```sql
SELECT COUNT(DISTINCT relates_to_id)::bigint AS fact_documents FROM fact_egisz_transactions;
SELECT COUNT(*)::bigint AS stg_outbound FROM stg_egisz_outbound_documents;
SELECT COUNT(DISTINCT document_group_key)::bigint AS parse_error_documents FROM v_stg_parse_errors_by_document;
```

**Логи ETL:** в UI или выводе CLI смотрите **`fetched`**, **`facts_upserted`** — они **не** равны числу строк журнала (часть строк не даёт факт, тестовые клиники, **`stg_parse_errors`**).

## Конфигурация и доступы

Параметры: шаблон [`config/egisz_monitor.example.yaml`](config/egisz_monitor.example.yaml); рабочий файл — `config/egisz_monitor.yaml` или путь из `EGISZ_MONITOR_CONFIG`. В Kubernetes — [`k8s/local/egisz_monitor.yaml`](k8s/local/egisz_monitor.yaml).

| Компонент | Типовые значения (локально) |
|-----------|-----------------------------|
| Firebird | С пода k8s на Windows: **`host.docker.internal:3050`**; алиас/БД из конфига; часто **`SYSDBA`** / **`masterkey`**; **`WIN1251`** |
| PostgreSQL | **`postgres.egisz-monitor.svc.cluster.local:5432`**; БД **`egisz_reports`**; пользователь **`egisz`** / пароль из секрета |
| Metabase | Админ **`admin@egisz.local`** / **`egisz`**; UI **`http://127.0.0.1:3000`** (совпадайте с **`MB_SITE_URL`** и браузером) |

## Локальная инфраструктура

Namespace: **`egisz-monitor`**. Быстрый старт с хоста Windows (по умолчанию применяются текущие манифесты и конфиг **без** сброса БД Metabase):

```powershell
.\start.ps1
```

Первичная установка с пересборкой обоих образов и **DROP/CREATE** БД приложения Metabase в Postgres: **`.\start.ps1 -Action deploy`** (подробности — [`docs/BI_EGISZ_INFOKLINIKA_AUDIT.md`](docs/BI_EGISZ_INFOKLINIKA_AUDIT.md) §8).

| Сервис | Доступ | Назначение |
|--------|--------|------------|
| PostgreSQL | `postgres:5432` в кластере (ClusterIP); с хоста — **`kubectl port-forward svc/postgres 5432:5432`** | Витрина **`egisz_reports`**, БД приложения Metabase |
| Metabase | Service **`metabase`**, порт **3000**; на Docker Desktop **LoadBalancer** → часто **`http://127.0.0.1:3000`** | Дашборды |
| Config UI | Service **`conf-ui`**, порт **8080**; LoadBalancer → часто **`http://127.0.0.1:8080`** | Конфиг, sync, healthcheck API |
| Airflow | Helm / `k8s/airflow/` | Опциональный планировщик |

### Манифесты в `k8s/`

| Файл | Назначение |
|------|------------|
| `k8s/metabase.yaml` | Service **`metabase`** (LoadBalancer :3000), Deployment, образ **`egisz-monitor-metabase`** (тег в YAML) |
| `k8s/conf-ui.yaml` | Service **`conf-ui`**, Deployment **`conf-ui`**, образ **`egisz-conf-ui:sync-web`**, SA/RBAC для CronJob |
| `k8s/etl-cron.yaml` | CronJob **`egisz-monitor-sync`** (`egisz-monitor sync`); `suspend` / `schedule` / `timeZone` выравниваются с `auto_sync` в YAML (UI, `start.ps1`) |
| `k8s/metabase-admin-secret.example.yaml` | Шаблон Secret **`metabase-admin`** (рабочая копия создаётся `start.ps1`; не коммитить прод-секреты) |
| `k8s/postgres/` | StatefulSet Postgres (Service ClusterIP), Job схемы витрины и БД приложения Metabase |
| `k8s/local/egisz_monitor.yaml` | Пример фрагмента конфига для Secret Config UI |

Полные сценарии **`start.ps1`** (`deploy`, `apply`, `reset-deploy`, port-forward) — [`docs/BI_EGISZ_INFOKLINIKA_AUDIT.md`](docs/BI_EGISZ_INFOKLINIKA_AUDIT.md) §8.

Если **LoadBalancer** в состоянии Pending (например **kind**), используйте **`kubectl port-forward`** (см. [`docs/BI_EGISZ_INFOKLINIKA_AUDIT.md`](docs/BI_EGISZ_INFOKLINIKA_AUDIT.md) §8.4) или полный **`.\start.ps1`** / **`apply`** (скрипт поднимает 8080/3000). Полный список действий: **`.\start.ps1 -Action help`**.
