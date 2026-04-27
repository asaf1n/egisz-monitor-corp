## EGISZ Monitor Corp

**EGISZ Monitor Corp** — корпоративный ETL-сервис для централизованного мониторинга и анализа процесса обмена данными между Медицинскими Информационными Системами (МИС) и федеральными сервисами ЕГИЗС (РЭМД). Система обеспечивает сквозную прослеживаемость документов через сбор данных из Firebird, парсинг SOAP-ответов и формирование аналитических витрин в PostgreSQL.

### Выборка данных (Sampling)
Извлечение данных из Firebird реализовано по принципу инкрементальной дозагрузки:
* **Механизм смещения (Offset):** Основной цикл ETL опирается на поле **LOGID**. Контрольная точка (last_id) считывается из таблицы `etl_state` в PostgreSQL и обновляется после обработки каждого пакета данных.
* **Ограничение выборки:** Для исключения избыточного сканирования применяется условие `LOGDATE >= CURRENT_TIMESTAMP - sync_window_days`.
* **Пакетная обработка:** Данные запрашиваются батчами через `SELECT FIRST {batch_size}` с обязательной сортировкой по `LOGID`.

### Кэширование и оптимизация
Для минимизации нагрузки на источник данных сервис использует механизм предварительной загрузки справочников:
* **Объекты кэширования:** При старте задачи синхронизации из Firebird извлекаются все записи таблиц `EGISZ_LICENSES` и `JPERSONS`.
* **Место и тип памяти:** Справочники (маппинги OID к JID, ИНН и наименования клиник) сохраняются непосредственно в **оперативной памяти (RAM)** процесса выполнения в виде Python-структур: словарей (`dict`) для быстрого поиска $O(1)$ и списков (`list`). Это исключает необходимость повторных запросов к Firebird или PostgreSQL в основном цикле обработки каждой транзакции.

### Логика обработки и идентификации (ETL логика)
Процесс разбора сообщений направлен на установление точной связи между асинхронным ответом ЕГИЗС и медицинским объектом в МИС.

**Определение документа:**
* Идентификатор `<localUid>` извлекается из SOAP-ответа в поле **`EXCHANGELOG.MSGTEXT`** и сопоставляется с полем **`EGISZ_MESSAGES.DOCUMENTID`**. Связующим звеном выступает тег `relatesToMessage`.

**Определение ЮЛ Клиники:**
Идентификация организации выполняется по приоритетной схеме:
1.  **По хосту:** `JID` извлекается из строки хоста в поле **`EXCHANGELOG.LOGTEXT`** по маске `http://gost-<jid>.infoclinica.lan`.
2.  **По OID:** Значение тега `<organization>` из XML-ответа сопоставляется с **`EGISZ_LICENSES.MO_UID`** для получения `JID`.
3.  **По наименованию:** Итоговое название клиники подтягивается из таблицы **`JPERSONS`** по ключу `JID`.

### Интерпретация данных и сопоставления (Mappings)

| Поле в DWH | Источник (FB / XML) | Описание и бизнес-логика |
| :--- | :--- | :--- |
| **`relates_to_id`** | `<relatesToMessage>` (MSGTEXT) | **Ключ связи.** Технический ID, связывающий асинхронный ответ ЕГИЗС с исходным запросом МИС. |
| **`local_uid_semd`** | `<localUid>` (MSGTEXT) / `DOCUMENTID` | Идентификатор документа. Значение из XML-ответа приоритетнее данных из таблицы `EGISZ_MESSAGES`. |
| **`jid`** | `gost-` в LOGTEXT / `MO_UID` / `MO_DOMEN` | **ID клиники.** Разрешается через: 1) Токен хоста; 2) Поле `JID` в `EGISZ_LICENSES`; 3) Маппинг OID на лицензию. |
| **`status`** | `<status>` (MSGTEXT) | Результат обработки: `success` (успех), `error` (ошибка) или `unknown`. |
| **`errors_json`** | `<errors>` (MSGTEXT) | Массив кодов и текстов ошибок РЭМД для технического анализа причин отказа в регистрации. В представлении `*_ui` дополнительно колонка **«Сводка ошибок»** (SQL-агрегат, исходный JSON не меняется). |

### Описание отчётов Metabase

| Дашборд / Отчёт | Описание логики и бизнес-применения | SQL / фильтры (актуальные запросы в `metabase_dashboards/*.json`) |
| :--- | :--- | :--- |
| **01 Оперативный мониторинг** | **Контроль текущего состояния.** Визуализирует распределение статусов (успех/ошибка), топы по типам СЭМД и клиникам. | `SELECT "Статус", COUNT(*)::bigint AS "Количество" FROM public.v_egisz_transactions_enriched_ui WHERE "Обработано" >= NOW() - INTERVAL '24 hours' GROUP BY 1` |
| **02 Сервис интеграции** | **Анализ структуры потока.** Разбивка транзакций по конкретным типам СЭМД и медицинским организациям. | `SELECT "Код СЭМД", "Наименование СЭМД", "JID клиники", "Наименование клиники", COUNT(*)::bigint AS "Количество" FROM public.v_egisz_transactions_enriched_ui GROUP BY 1, 2, 3, 4` |
| **03 Ошибки и разбор** | **Технический аудит.** Вывод реестра ошибок парсинга и регистрации для анализа конкретных причин отказов. | `SELECT id, error_code, LEFT(message, 200), created_at FROM public.stg_parse_errors ORDER BY id DESC LIMIT 100` |
| **04 Документы без ответа** | **Поиск «зависших» транзакций.** Анализ документов, по которым не поступил подтверждающий callback от ЕГИЗС. | `SELECT * FROM public.v_rpt_documents_no_response_ui ORDER BY "Отправлено" DESC NULLS LAST LIMIT 100` |
| **05 Тренды и динамика** | **Анализ нагрузки.** Временные ряды объемов передачи данных и динамика изменения доли ошибок по дням/часам. | `SELECT DATE(COALESCE("Дата регистрации", "Обработано")) AS "Дата", "Статус", COUNT(*)::bigint FROM public.v_egisz_transactions_enriched_ui GROUP BY 1, 2` |
| **06 Качество данных** | **Контроль полноты.** Проверка корректности маппинга справочников и заполнения обязательных атрибутов СЭМД. | `SELECT (SELECT COUNT(*)::bigint FROM public.v_egisz_transactions_enriched_ui WHERE NULLIF(BTRIM("JID клиники"::text), '') IS NULL) AS "Транз. без JID", (SELECT COUNT(*)::bigint FROM public.v_egisz_transactions_enriched_ui WHERE NULLIF(BTRIM("OID организации"::text), '') IS NOT NULL AND NULLIF(BTRIM("OID клиники"::text), '') IS NOT NULL AND BTRIM(COALESCE("OID организации"::text, '')) <> BTRIM(COALESCE("OID клиники"::text, ''))) AS "Несовпадение OID"` |
| **07 Глубокий анализ ошибок** | **Классификация инцидентов.** Топы и срезы по смысловой сводке (`egisz_friendly_error_item` / «Сводка ошибки»), без подмены сырого `message` в JSON. | См. `metabase_dashboards/07_errors_deep.json` — `public.egisz_friendly_error_item(code, message)` |
| **08 Агрегация ожидающих** | **Мониторинг очереди.** Сводные данные по документам, находящимся в промежуточных статусах обработки. | `SELECT "JID клиники", "Наименование клиники", COUNT(*)::bigint FROM public.v_rpt_documents_no_response_ui GROUP BY 1, 2` |
| **09 Управленческий дашборд** | **Executive Summary.** Агрегированные KPI за сегодня: общее кол-во, % ошибок и размер текущей очереди. | `SELECT (SELECT COUNT(*) FROM public.v_egisz_transactions_enriched_ui t WHERE t."Обработано" >= date_trunc('day', NOW()) AND t."Обработано" < date_trunc('day', NOW()) + INTERVAL '1 day'), (SELECT ROUND(100*SUM(CASE WHEN "Статус"='error' THEN 1 ELSE 0 END)/NULLIF(COUNT(*),0),2) FROM public.v_egisz_transactions_enriched_ui t2 WHERE t2."Обработано" >= date_trunc('day', NOW()) AND t2."Обработано" < date_trunc('day', NOW()) + INTERVAL '1 day'), (SELECT COUNT(*) FROM public.v_rpt_documents_no_response_ui))` |

В UI Metabase имена дашбордов: **«01 Оперативный мониторинг»** … **«09 Управленческий дашборд»**. Сброс БД приложения Metabase (`metabase` в Postgres, витрина не затрагивается) входит в **`.\start.ps1 -Action deploy`** и **`reset-deploy`**. Отдельно, без полного деплоя: `.\start.ps1 -Action build` при смене JSON дашбордов, затем `.\start.ps1 -Action reset-metabase`. **`apply`** манифесты применяет **без** DROP/CREATE Metabase.

### Конфигурация и доступы (Примеры)
Параметры соединений управляются через файл `config/egisz_corp.yaml` или переменные окружения.

**Примеры из конфигураций проекта:**
* **Firebird (Источник):** `SYSDBA` / `masterkey` (алиас/путь на сервере FB — типично `proxy_egisz`, см. `aliases.conf`; с подов K8s на Windows: `host.docker.internal:3050`, см. `k8s/local/egisz_corp.yaml`).
* **PostgreSQL (DWH):** только в Kubernetes (`postgres.egisz-corp.svc.cluster.local:5432`, БД `egisz_reports`, пользователь `egisz` / `egisz`).
* **Metabase (Setup):** `admin@egisz.local` / `egisz`.

Краткий порядок **сбора данных → витрина → Metabase** (схема Postgres, ETL, образ Metabase с JSON): [`docs/METABASE.md`](docs/METABASE.md) § «Сбор данных в витрину и использование Metabase».

### Инфраструктура
| Сервис | Адрес в K8s | Назначение |
| :--- | :--- | :--- |
| **PostgreSQL** | `postgres:5432` | Основное хранилище витрин данных. |
| **Metabase** | `metabase:3000` | Аналитическая платформа и дашборды. |
| **Config UI** | `conf-ui:8080` | Веб-интерфейс конфигурации и запуск синхронизации ETL (`egisz-corp sync`). |
| **Airflow** | `airflow-webserver` | Планировщик и мониторинг ETL-задач (опционально, см. `k8s/airflow/`). |