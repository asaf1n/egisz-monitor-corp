## EGISZ Monitor Corp

**EGISZ Monitor Corp** — сервис для мониторинга обмена между медицинскими информационными системами и федеральным контуром ЕГИЗС / РЭМД. Он читает журнал Firebird, разбирает SOAP-ответы, сохраняет результат в PostgreSQL и отдаёт готовые витрины в Metabase.

Основной поток данных:

```text
Firebird EXCHANGELOG / EGISZ_MESSAGES
  → парсинг MSGTEXT и транспортных полей
  → PostgreSQL: fact_egisz_transactions, справочники, отчёты
  → Metabase: преднастроенные дашборды
```

Смежные документы:

- **`AGENTS.md`** — структура проекта и подсказки для разработки.
- **`.cursorrules`** — доменная логика: СЭМД, статусы, сигналы тревоги, интерпретация отчётов.
- **`docs/INTEGRATION_AUDIT.md`** — аудит сервиса (3 фокуса: техника/масштабируемость, бизнес-применение, healthcheck).
- **`docs/METABASE.md`** — провижининг Metabase, фильтры дат, обновление дашбордов.
- **`docs/KUBERNETES_LOCAL.md`** — локальный Kubernetes и сценарии `start.ps1`.
- **`docs/SYNC_DIAGNOSTICS.md`** — сверка объёмов Firebird и PostgreSQL, проверка курсора синхронизации.

### Стек

| Слой | Используется |
| :--- | :--- |
| Язык | Python 3.10 и новее |
| Источник данных | Firebird, пакет `firebird-driver`, нативный клиент `fbclient` / переменная `FB_CLIENT_LIBRARY` |
| Хранилище витрины | PostgreSQL, пакет `psycopg2-binary` |
| Конфигурация | YAML через `PyYAML`; файл `config/egisz_corp.yaml` или путь из `EGISZ_CORP_CONFIG` |
| Веб-интерфейс | Flask Config UI, ручной запуск синхронизации через `sync_routes.py` |
| Планировщик | Apache Airflow, DAG `egisz_corp_firebird_to_postgres` |
| Аналитика | Metabase поверх PostgreSQL, дашборды хранятся в `metabase_dashboards/*.json` |

Команды пакета зарегистрированы в `pyproject.toml`: **`egisz-corp`** и **`egisz-monitor`** ведут в один CLI-модуль `egisz_monitor_corp.cli`.

### Синхронизация Firebird → PostgreSQL

Главная процедура — **`run_sync`** в `egisz_monitor_corp.etl`. Она читает Firebird только `SELECT`-запросами и обновляет витрину в PostgreSQL.

Инкремент строится по полю **`EXCHANGELOG.LOGID`**. Последний обработанный идентификатор хранится в `etl_state.last_log_id` для пайплайна из конфигурации, по умолчанию `firebird_exchangelog`. Если включить `full_scan: true`, курсор начинается с нуля, но выборка всё равно ограничивается настроенным окном `sync_window_days`.

Один прогон выполняет следующие шаги:

1. Загружает справочники `EGISZ_LICENSES` и `JPERSONS` в память процесса.
2. Считает количество строк `EXCHANGELOG` после курсора для прогресса в интерфейсе. Ошибка этого подсчёта не останавливает синхронизацию.
3. Читает журнал страницами: `SELECT FIRST {batch_size}` с условием `e.LOGID > {last_id}` и сортировкой по `LOGID`.
4. Разбирает `MSGTEXT` как источник SOAP/XML и использует `LOGTEXT` / `REPLYTO` как транспортные поля для поиска клиники.
5. Записывает факты и измерения через UPSERT. Ошибки разбора попадают в `stg_parse_errors`.
6. После страницы двигает `etl_state.last_log_id` до максимального обработанного `LOGID`.
7. Отдельно обновляет `stg_egisz_outbound_documents` для отчёта «Документы без ответа».

Запустить тот же код можно несколькими способами:

| Способ | Что происходит |
| :--- | :--- |
| `egisz-corp sync` | CLI-запуск синхронизации; конфиг берётся из `--config` или `EGISZ_CORP_CONFIG`. |
| Config UI | Flask запускает `run_sync` в фоновом потоке. Повторный старт во время активной синхронизации отклоняется. |
| Apache Airflow | DAG сначала проверяет соединения, затем вызывает `run_sync`. |
| `kubectl exec deploy/conf-ui -- egisz-monitor sync` | Ручной запуск внутри Kubernetes-пода `conf-ui`. |

`start.ps1` поднимает инфраструктуру и применяет схему, но полный прогон ETL не встроен в `deploy` / `apply` по умолчанию.

### Выборка и кэш справочников

Окно журнала ограничивается по **`LOGDATE`**: в типовом SQL используется `DATEADD(-sync_window_days DAY TO CURRENT_TIMESTAMP)`. Пакетная обработка идёт по страницам размера `batch_size`, курсор сдвигается после каждой страницы.

Перед основным циклом сервис загружает строки `EGISZ_LICENSES` и `JPERSONS` с непустым `JID`. В памяти строятся словари и списки для быстрого поиска `MO_UID → JID`, названия клиники, ИНН и `FIR_OID`. Поэтому каждая транзакция не делает отдельные запросы к Firebird за справочными данными.

### Разбор сообщения и идентификация

Факт строится только тогда, когда из SOAP-ответа можно получить связь с исходящим запросом:

- **`relates_to_id`** берётся из `<relatesToMessage>` в XML из `EXCHANGELOG.MSGTEXT`. Если связи нет, строка не попадает в факт и фиксируется в `stg_parse_errors`.
- **`local_uid_semd`** сначала берётся из `<localUid>` в XML. Если его нет, используется `EGISZ_MESSAGES.DOCUMENTID`.
- **`status`** нормализуется в `success`, `error` или `unknown`.
- **`errors_json`** сохраняет массив `<errors>` из ответа РЭМД без подмены исходного текста.

Порядок определения клиники:

1. Токен `gost-<jid>.infoclinica.lan` ищется в `MSGTEXT`, затем в `LOGTEXT`, затем в `EGISZ_MESSAGES.REPLYTO`.
2. Если токен не дал числовой `JID`, используется `JID` из строки `EGISZ_LICENSES`, найденной SQL-запросом по `REPLYTO ↔ MO_DOMEN`.
3. Если `JID` всё ещё не найден, `<organization>` из XML или `MO_UID` строки лицензии сопоставляется с предзагруженной картой `MO_UID → JID`.
4. Наименование, ИНН и `FIR_OID` клиники дополняются из `JPERSONS` и `EGISZ_LICENSES`.

### Основные поля витрины

| Поле | Источник | Смысл |
| :--- | :--- | :--- |
| `relates_to_id` | `<relatesToMessage>` из `MSGTEXT` | Ключ факта и связь асинхронного ответа с исходящим запросом. |
| `local_uid_semd` | `<localUid>` из `MSGTEXT`, иначе `EGISZ_MESSAGES.DOCUMENTID` | Идентификатор экземпляра СЭМД. Используется для поиска документов без ответа. |
| `jid` | `gost-` в `MSGTEXT` / `LOGTEXT` / `REPLYTO`, затем `EGISZ_LICENSES`, затем `MO_UID` | Внутренний идентификатор клиники. |
| `kind_code` | `<kind>` из XML, иначе `EGISZ_LICENSES.KIND` | Код типа СЭМД. В `*_ui` приведён к тексту, чтобы Metabase не суммировал его как число. |
| `status` | `<status>` из XML | Результат регистрации: `success`, `error` или `unknown`. |
| `errors_json` | `<errors>` из XML | Сырые коды и тексты отказов РЭМД. |
| `errors_friendly` / «Сводка ошибок» | SQL-функции `egisz_friendly_error_item` и `egisz_friendly_errors_row` | Человекочитаемая строка для отчётов. Исходный `errors_json` сохраняется отдельно. |

Полное описание схемы, представлений и комментариев находится в `sql/001_schema.sql`.

### Metabase

Дашборды задаются JSON-файлами в `metabase_dashboards/`. При старте пода Metabase скрипт `metabase/provision.sh` вызывает `setup-dashboards.sh` и создаёт дашборды в личной коллекции администратора.

| Файл | Дашборд | Основной вопрос |
| :--- | :--- | :--- |
| `01_operational.json` | `01 Оперативный мониторинг` | Последние операции, статусы, СЭМД и клиники. |
| `02_service.json` | `02 Сервис интеграции` | Структура потока по типам СЭМД и медицинским организациям. |
| `03_errors.json` | `03 Ошибки и разбор` | Ошибки парсинга из `stg_parse_errors` и детали отказов РЭМД. |
| `04_documents_no_response.json` | `04 Документы без ответа` | Исходящие документы без callback с тем же `localUid`. |
| `05_trends.json` | `05 Тренды и динамика` | Объём и доля ошибок по дням, типам СЭМД и часам. |
| `06_quality.json` | `06 Качество данных` | Полнота маппинга и обязательных атрибутов. |
| `07_errors_deep.json` | `07 Глубокий анализ ошибок` | Классификация отказов РЭМД через `egisz_friendly_error_item`. |
| `08_pending_agg.json` | `08 Агрегация ожидающих` | Очередь «без ответа» по клиникам, СЭМД и возрасту ожидания. |
| `09_executive.json` | `09 Управленческий дашборд` | Руководительские показатели: объём, доля ошибок, очередь, рейтинги. |
| `10_errors_top.json` | `10 Топы ошибок` | Топы формулировок отказов, кодов СЭМД и клиник по статусу `error`. |
| `11_healthcheck.json` | `11 Healthcheck интеграции` | Сигналы (`v_health_signals`), тепловая карта клиники × дни (доля ошибок), age-buckets очереди, тренд parse-errors, сводка прокси-БД. |

Большинство карточек читают `v_egisz_transactions_enriched_ui` и `v_rpt_documents_no_response_ui`, где колонки уже имеют русские подписи. Для анализа отказов дашборды `07`, `09` и `10` используют первый значимый элемент массива «Ошибки JSON» на транзакцию, чтобы один документ не попадал в рейтинг причин несколько раз. Healthcheck-витрина (`11`) читает `v_health_*_ui` поверх `sql/005_healthcheck.sql`.

Обновление дашбордов:

- `.\start.ps1 -Action deploy` и `reset-deploy` пересоздают базу приложения Metabase `metabase` и заново провижинят дашборды.
- `.\start.ps1 -Action apply` применяет манифесты и перезапускает сервисы, но сохраняет существующую базу приложения Metabase.
- После изменения JSON дашбордов нужен новый образ Metabase: `.\start.ps1 -Action build`, затем перезапуск Metabase или `reset-metabase` (тег `:k8s-v13` уже зашит в `start.ps1` и `k8s/metabase.yaml`; **bump** при следующем изменении JSON или скриптов).
- Обновление только схемы витрины или данных ETL не требует пересборки образа Metabase.

### Healthcheck сервиса интеграции

Сервис мониторит «здоровье» интеграции **массово по клиникам и по прокси-БД** через три источника, синхронизированные между собой:

1. **SQL-витрина** `sql/005_healthcheck.sql` — три представления (`v_health_by_clinic`, `v_health_signals`, `v_health_proxy_db`) и UI-обёртки `*_ui`. Применяется в Job `egisz-reports-schema-init` и в каждом запуске `run_sync` (идемпотентно).
2. **Эндпоинт** `GET /api/healthcheck` в Config UI — JSON-снимок с массивами `signals`, `by_clinic_top`, объектом `proxy_db` и сводкой по уровням `level_summary`. Запрос ограничен `statement_timeout = 10s`, при недоступной PG возвращается `{"ok": false, "errors": [...]}` со статусом 200 (graceful).
3. **Дашборд** `11_healthcheck.json` в Metabase — карточки сигналов, тепловая карта клиники × дни (доля ошибок), age-buckets очереди, тренд `stg_parse_errors`, сводка прокси-БД.
4. **Config UI**: правая панель содержит две вкладки — **Snapshot** (текущие `EGMID/LOGID/MODIFYDATE` Firebird и курсор PG) и **Healthcheck** (сигналы, top-3 проблемные клиники, сводка прокси-БД). Healthcheck-вкладка опрашивает `/api/healthcheck` каждые 30 секунд.

Сигналы и пороги (по умолчанию):

| Сигнал | Условие | Уровень |
| :--- | :--- | :--- |
| `error_rate_high` | error-rate за 24ч > 10% при объёме ≥ 50 | red |
| `unknown_high` | unknown за 24ч > 5% при объёме ≥ 20 | yellow |
| `parse_errors_burst` | parse_errors за 1ч > 10 | red |
| `queue_red_24h` | в очереди > 24ч больше 50 документов | red |
| `cursor_stale` | `etl_state.updated_at` старше 6 ч | red |

Подробное обоснование порогов и сценарии триажа — в `docs/INTEGRATION_AUDIT.md` §3.

### Конфигурация и доступы

Параметры соединений хранятся в `config/egisz_corp.yaml`, в Kubernetes-примере — в `k8s/local/egisz_corp.yaml`.

Примеры из репозитория:

| Компонент | Значения по умолчанию |
| :--- | :--- |
| Firebird | `host.docker.internal:3050` из пода Kubernetes на Windows; база или алиас `proxy_egisz`; пользователь `SYSDBA`; пароль `masterkey`; кодировка `WIN1251`. |
| PostgreSQL | `postgres.egisz-monitor.svc.cluster.local:5432`; база `egisz_reports`; пользователь `egisz`; пароль `egisz`; схема `public`. |
| Metabase | Администратор `admin@egisz.local`; пароль `egisz`; локальный адрес обычно `http://127.0.0.1:3000` или `http://localhost:3000` в зависимости от способа доступа. |

### Локальная инфраструктура

Манифесты Kubernetes рассчитаны на namespace **`egisz-monitor`**. Основной сценарий запуска с Windows-хоста:

```powershell
.\start.ps1 -Action deploy
```

Ключевые сервисы:

| Сервис | Адрес внутри namespace | Назначение |
| :--- | :--- | :--- |
| PostgreSQL | `postgres:5432` | Хранилище витрины `egisz_reports` и база приложения Metabase. |
| Metabase | `metabase:3000` | Аналитический интерфейс и дашборды. |
| Config UI | `conf-ui:8080` | Flask-интерфейс конфигурации и ручного запуска `run_sync`. |
| Airflow | зависит от Helm chart | Опциональный планировщик ETL, см. `k8s/airflow/`. |

Краткая справка по действиям `start.ps1` есть в `docs/KUBERNETES_LOCAL.md`, полный список — `.\start.ps1 -Action help`.