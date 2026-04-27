## EGISZ Monitor Corp

**EGISZ Monitor Corp** — корпоративный ETL-сервис для централизованного мониторинга и анализа процесса обмена данными между Медицинскими Информационными Системами (МИС) и федеральными сервисами ЕГИЗС (РЭМД). Система обеспечивает сквозную прослеживаемость документов через сбор данных из Firebird, парсинг SOAP-ответов и формирование аналитических витрин в PostgreSQL.

### Стек технологий

| Слой | Технологии |
| :--- | :--- |
| Язык | Python 3.10+ |
| Источник данных | **Firebird** через пакет **firebird-driver** (нужен нативный клиент **fbclient** / **FB_CLIENT_LIBRARY**) |
| Хранилище (DWH) | **PostgreSQL** через **psycopg2-binary** |
| Конфигурация | **PyYAML**, YAML (`config/egisz_corp.yaml`; путь задаётся переменной **EGISZ_CORP_CONFIG**) |
| Веб | **Flask** (в т.ч. ручной запуск синхронизации из UI — `sync_routes.py`) |
| Оркестрация | **Apache Airflow** — DAG `egisz_corp_firebird_to_postgres` вызывает `run_sync` |
| Аналитика | **Metabase** поверх PostgreSQL (преднастроенные дашборды в репозитории) |

Точка входа CLI: **`egisz-corp`** → `egisz_monitor_corp.cli`.

### Синхронизация

Центральная процедура — **`run_sync`** в модуле `egisz_monitor_corp.etl`: перенос данных **Firebird → PostgreSQL**; источник читается **SELECT**-запросами через **firebird-driver**, витрина обновляется в PostgreSQL.

**Инкремент и курсор:** водяной знак — поле **`LOGID`** в `EXCHANGELOG`. В PostgreSQL в таблице **`etl_state`** хранится **`last_log_id`** для имени пайплайна из конфигурации (по умолчанию `firebird_exchangelog`). При **`full_scan: true`** курсор сбрасывается и выполняется полный проход в рамках настроенного окна. В обычном режиме читаются строки с **`LOGID` больше сохранённого курсора**.

**Порядок шага синхронизации:** (1) из Firebird подгружаются справочники **`EGISZ_LICENSES`** и **`JPERSONS`** (два запроса) для обогащения и маппинга в памяти процесса; (2) выполняется **COUNT** строк журнала после курсора для прогресса в UI (best-effort: при сбое COUNT синхронизация продолжается, см. лог); (3) **`EXCHANGELOG`** выбирается **постранично** (`batch_size`, обёртка `SELECT FIRST …` с сортировкой по `LOGID` в `sql_util.paginated_exchangelog_sql`); (4) для каждой строки парсер разбирает **`MSGTEXT`** / **`LOGTEXT`**, ошибки парсинга пишутся в **`stg_parse_errors`** в PG; (5) факты и измерения **UPSERT** в витрину, после обработки пакета курсор **`last_log_id`** обновляется до максимального **`LOGID`** на странице; (6) отдельным запросом к Firebird обновляется staging **исходящих документов** (`stg_egisz_outbound_documents` / отчёт «без ответа»).

**Запуск:** по расписанию — DAG **Apache Airflow** `egisz_corp_firebird_to_postgres` (задача вызывает `run_sync` с путём конфига из переменных Airflow); вручную — **Flask** Config UI через **`sync_routes`** (фоновый поток, single-flight: повторный запуск, пока идёт синк, отклоняется). Проверка соединений с источником и DWH может выполняться отдельной задачей DAG до синхронизации.

### Выборка данных (Sampling)
Извлечение данных из Firebird реализовано по принципу инкрементальной дозагрузки:
* **Курсор (watermark) по LOGID:** в **`etl_state`** хранится последний обработанный **`LOGID`**; после каждого пакета значение сдвигается вперёд до максимума **`LOGID`** на обработанной странице.
* **Ограничение выборки (окно по дате):** в типовом SQL журнала строки ограничиваются по **`LOGDATE`** (в коде: `DATEADD(-sync_window_days DAY TO CURRENT_TIMESTAMP)` во Firebird, эквивалент «последние N дней»).
* **Пакетная обработка:** страница журнала — внешний `SELECT FIRST {batch_size}` подзапроса с `… AND e.LOGID > {last_id} ORDER BY e.LOGID` (`paginated_exchangelog_sql`).

### Кэширование и оптимизация
Для минимизации нагрузки на источник данных сервис использует механизм предварительной загрузки справочников:
* **Объекты кэширования:** При старте задачи синхронизации из Firebird выбираются строки **`EGISZ_LICENSES`** с непустым **`JID`** и строки **`JPERSONS`** с непустым **`JID`** (см. `enrichment_*_sql` в `sql_util.py`).
* **Место и тип памяти:** Справочники (маппинги OID к JID, ИНН и наименования клиник) сохраняются непосредственно в **оперативной памяти (RAM)** процесса выполнения в виде Python-структур: словарей (`dict`) для быстрого поиска $O(1)$ и списков (`list`). Это исключает необходимость повторных запросов к Firebird или PostgreSQL в основном цикле обработки каждой транзакции.

### Логика обработки и идентификации (ETL логика)
Процесс разбора сообщений направлен на установление точной связи между асинхронным ответом ЕГИЗС и медицинским объектом в МИС.

**Определение документа и связи с запросом:**
* Асинхронная связь ответа с исходным сообщением задаётся тегом **`<relatesToMessage>`** в XML из **`EXCHANGELOG.MSGTEXT`** → поле витрины **`relates_to_id`**. События, для которых нельзя построить факт (парсинг, отсутствие связи), фиксируются в **`stg_parse_errors`**.
* Идентификатор документа в витрине **`local_uid_semd`**: сначала **`<localUid>`** из того же XML, иначе подставляется **`EGISZ_MESSAGES.DOCUMENTID`** из джойна к журналу.

**Определение ЮЛ / `JID` клиники (порядок в `resolve_clinic`):**
1. **По URL / REPLYTO:** токен **`gost-<jid>.infoclinica.lan`** ищется регулярным выражением в **`EXCHANGELOG.LOGTEXT`**, затем в **`EGISZ_MESSAGES.REPLYTO`**.
2. **По строке лицензии в выборке:** **`JID`** из подзапроса **`EGISZ_LICENSES`** (сопоставление **`REPLYTO`** с **`MO_DOMEN`** уже в SQL журнала).
3. **По OID:** текст тега **`<organization>`** в XML (или **`MO_UID`** из той же строки лицензии) сопоставляется с картой **`MO_UID` → `JID`** из предзагруженного **`EGISZ_LICENSES`**.
4. **Наименование и реквизиты** из **`JPERSONS`** подставляются в измерения по уже разрешённому **`JID`**.

### Интерпретация данных и сопоставления (Mappings)

| Поле в DWH | Источник (FB / XML) | Описание и бизнес-логика |
| :--- | :--- | :--- |
| **`relates_to_id`** | `<relatesToMessage>` (MSGTEXT) | **Ключ связи.** Технический ID, связывающий асинхронный ответ ЕГИЗС с исходным запросом МИС. |
| **`local_uid_semd`** | `<localUid>` (MSGTEXT) / `DOCUMENTID` | Идентификатор документа. Значение из XML-ответа приоритетнее данных из таблицы `EGISZ_MESSAGES`. |
| **`jid`** | `gost-` в LOGTEXT / REPLYTO; `JID` и `MO_UID` из строки `EGISZ_LICENSES` (SQL по `REPLYTO`↔`MO_DOMEN`) | **ID клиники.** Порядок разрешения: URL-токен → `JID` из лицензии строки журнала → `MO_UID`/`<organization>` → `JID` по предзагруженной карте. |
| **`status`** | `<status>` (MSGTEXT) | Результат обработки: `success` (успех), `error` (ошибка) или `unknown`. |
| **`errors_json`** | `<errors>` (MSGTEXT) | Массив кодов и текстов ошибок РЭМД для технического анализа причин отказа в регистрации. В представлении `*_ui` колонка **«Сводка ошибок»** — SQL-агрегат по этому массиву; в факте хранится исходный JSON. |

### Описание отчётов Metabase

| Дашборд / Отчёт | Описание логики и бизнес-применения | SQL / фильтры (актуальные запросы в `metabase_dashboards/*.json`) |
| :--- | :--- | :--- |
| **01 Оперативный мониторинг** | **Контроль текущего состояния.** Визуализирует распределение статусов (успех/ошибка), топы по типам СЭМД и клиникам. | `SELECT "Статус", COUNT(*)::bigint AS "Количество" FROM public.v_egisz_transactions_enriched_ui WHERE "Обработано" >= NOW() - INTERVAL '24 hours' GROUP BY 1` |
| **02 Сервис интеграции** | **Анализ структуры потока.** Разбивка транзакций по конкретным типам СЭМД и медицинским организациям. | `SELECT "Код СЭМД", "Наименование СЭМД", "JID клиники", "Наименование клиники", COUNT(*)::bigint AS "Количество" FROM public.v_egisz_transactions_enriched_ui GROUP BY 1, 2, 3, 4` |
| **03 Ошибки и разбор** | **Технический аудит.** Реестр **`stg_parse_errors`**: ошибки разбора и привязки (`MISSING_RELATES_TO`, `XML_BROKEN` и т.п.). Отказы РЭМД по документам отражаются в **`errors_json`** витрины транзакций и в отчётах по фактам. | `SELECT id, error_code, LEFT(message, 200), created_at FROM public.stg_parse_errors ORDER BY id DESC LIMIT 100` |
| **04 Документы без ответа** | **Поиск «зависших» транзакций.** Очередь документов в ожидании подтверждающего callback от ЕГИЗС. | `SELECT * FROM public.v_rpt_documents_no_response_ui ORDER BY "Отправлено" DESC NULLS LAST LIMIT 100` |
| **05 Тренды и динамика** | **Анализ нагрузки.** Временные ряды объемов передачи данных и динамика изменения доли ошибок по дням/часам. | `SELECT DATE(COALESCE("Дата регистрации", "Обработано")) AS "Дата", "Статус", COUNT(*)::bigint FROM public.v_egisz_transactions_enriched_ui GROUP BY 1, 2` |
| **06 Качество данных** | **Контроль полноты.** Проверка корректности маппинга справочников и заполнения обязательных атрибутов СЭМД. | `SELECT (SELECT COUNT(*)::bigint FROM public.v_egisz_transactions_enriched_ui WHERE NULLIF(BTRIM("JID клиники"::text), '') IS NULL) AS "Транз. без JID", (SELECT COUNT(*)::bigint FROM public.v_egisz_transactions_enriched_ui WHERE NULLIF(BTRIM("OID организации"::text), '') IS NOT NULL AND NULLIF(BTRIM("OID клиники"::text), '') IS NOT NULL AND BTRIM(COALESCE("OID организации"::text, '')) <> BTRIM(COALESCE("OID клиники"::text, ''))) AS "Несовпадение OID"` |
| **07 Глубокий анализ ошибок** | **Классификация инцидентов.** Топы и срезы по смысловой сводке (`egisz_friendly_error_item` / «Сводка ошибки»); сырое поле **`message`** в JSON доступно для детального разбора. | См. `metabase_dashboards/07_errors_deep.json` — `public.egisz_friendly_error_item(code, message)` |
| **08 Агрегация ожидающих** | **Мониторинг очереди.** Сводные данные по документам, находящимся в промежуточных статусах обработки. | `SELECT "JID клиники", "Наименование клиники", COUNT(*)::bigint FROM public.v_rpt_documents_no_response_ui GROUP BY 1, 2` |
| **09 Управленческий дашборд** | **Executive Summary.** Агрегированные KPI за сегодня: общее кол-во, % ошибок и размер текущей очереди. | `SELECT (SELECT COUNT(*) FROM public.v_egisz_transactions_enriched_ui t WHERE t."Обработано" >= date_trunc('day', NOW()) AND t."Обработано" < date_trunc('day', NOW()) + INTERVAL '1 day'), (SELECT ROUND(100*SUM(CASE WHEN "Статус"='error' THEN 1 ELSE 0 END)/NULLIF(COUNT(*),0),2) FROM public.v_egisz_transactions_enriched_ui t2 WHERE t2."Обработано" >= date_trunc('day', NOW()) AND t2."Обработано" < date_trunc('day', NOW()) + INTERVAL '1 day'), (SELECT COUNT(*) FROM public.v_rpt_documents_no_response_ui))` |

В UI Metabase имена дашбордов: **«01 Оперативный мониторинг»** … **«09 Управленческий дашборд»**. Действия **`.\start.ps1 -Action deploy`** и **`reset-deploy`** пересоздают приложенческую БД Metabase (`metabase` в Postgres) и провижинят дашборды. Витрина **`egisz_reports`** обновляется схемой и ETL (Job **`egisz-reports-schema-init`**, **`egisz-corp apply-schema`**, **`egisz-corp sync`**). Для обновления дашбордов: `.\start.ps1 -Action build` при смене JSON, затем `.\start.ps1 -Action reset-metabase`. **`apply`** применяет манифесты и перезапускает сервисы; существующая БД приложения Metabase на кластере сохраняется.

### Конфигурация и доступы (Примеры)
Параметры соединений управляются через файл `config/egisz_corp.yaml` или переменные окружения.

**Примеры из конфигураций проекта:**
* **Firebird (Источник):** `SYSDBA` / `masterkey` (алиас/путь на сервере FB — типично `proxy_egisz`, см. `aliases.conf`; с подов K8s на Windows: `host.docker.internal:3050`, см. `k8s/local/egisz_corp.yaml`).
* **PostgreSQL (DWH):** в примере k8s — `postgres.egisz-corp.svc.cluster.local:5432`, БД `egisz_reports`, пользователь `egisz` / `egisz`.
* **Metabase (Setup):** `admin@egisz.local` / `egisz`.

Краткий порядок **сбора данных → витрина → Metabase** (схема Postgres, ETL, образ Metabase с JSON): [`docs/METABASE.md`](docs/METABASE.md) § «Сбор данных в витрину и использование Metabase».

### Инфраструктура
| Сервис | Адрес в K8s | Назначение |
| :--- | :--- | :--- |
| **PostgreSQL** | `postgres:5432` | Основное хранилище витрин данных. |
| **Metabase** | `metabase:3000` | Аналитическая платформа и дашборды. |
| **Config UI** | `conf-ui:8080` | Веб-интерфейс конфигурации (Flask); синхронизация вызывает **`run_sync`** в том же процессе, что и UI (HTTP API `sync_routes`), по смыслу совпадает с **`egisz-corp sync`**. |
| **Airflow** | сервис вроде `airflow-webserver` (зависит от Helm chart) | Планировщик ETL (опционально, см. `k8s/airflow/`). |