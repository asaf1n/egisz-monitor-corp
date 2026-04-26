# EGISZ Monitor Corp

**EGISZ Monitor Corp** — специализированный ETL-сервис для мониторинга интеграции Медицинских Информационных Систем (МИС) с реестрами ЕГИСЗ (в частности, РЭМД). Сервис выполняет извлечение данных из очередей Firebird, парсинг SOAP-ответов, нормализацию и загрузку в хранилище PostgreSQL для последующего анализа.

### Цели и задачи сервиса
* Автоматизированный сбор логов взаимодействия с ЕГИСЗ из СУБД Firebird.
* Десериализация SOAP-сообщений для извлечения статусов регистрации и идентификаторов документов.
* Формирование аналитических витрин данных в PostgreSQL для контроля качества интеграции.
* Предоставление инструментов для выявления ошибок парсинга и передачи данных.

### Стек технологий
* **Ядро:** Python 3.10+.
* **СУБД Источник:** Firebird (драйвер `firebird-driver`).
* **Хранилище (DWH):** PostgreSQL 15/16.
* **Оркестрация:** Apache Airflow (исполнение DAG `egisz_corp_firebird_to_postgres`).
* **Визуализация:** Metabase (карточки в `metabase_dashboards/`; подписи колонок и осей — кириллические алиасы в SQL и `visualization_settings`). **Кириллица в витрине:** драйвер Firebird должен декодировать исходную БД (`firebird.charset`, по умолчанию в коде и примерах — **`WIN1251`**); ETL пишет в PostgreSQL в **UTF-8** (`set_client_encoding` в `pg_warehouse.py`, локали в образах/k8s). После смены charset — полный прогон ETL. Подробнее: `docs/METABASE.md`.
* **Инфраструктура:** Docker, Kubernetes (namespace `egisz-corp`).

### Тесты и проверка стека

* **Юнит-тесты (Python):** каталог **`tests/`**; локально: **`.\start.ps1 -Action test`** (или `py -3 -m pip install -e ".[dev]"` и **`py -3 -m pytest`**).
* **Проверка витрины + Metabase в Kubernetes:** **`.\start.ps1 -Action verify`** (внутри пода Metabase: `metabase/verify-corp-stack.sh` — Postgres и число дашбордов в личной коллекции).

### Техническая реализация и логика парсинга

#### Инкрементальная загрузка
Синхронизация выполняется методом инкрементальной выборки. В качестве контрольной точки (watermark) используется поле `EXCHANGELOG.LOGID`, значение которого сохраняется в таблице `etl_state`.

#### Сопоставление данных (Mappings)
Для формирования записи в витрине данных `fact_egisz_transactions` используются следующие правила:

**1. Идентификация документа и корреляция:**
* **Связь с ответом:** Тег `<relatesToMessage>` из SOAP-ответа в **`EXCHANGELOG.MSGTEXT`** сохраняется как основной ключ `relates_to_id`.
* **localUid СЭМД:** Идентификатор документа `DOCUMENTID` (также передается как `localUid`) извлекается из таблицы **`EGISZ_MESSAGES`**.
* **Сбор `local_uid_semd` в витрине:** сначала читается тег `<localUid>` из SOAP (**`MSGTEXT`**); при отсутствии тега подставляется **`DOCUMENTID`** из строки журнала. **Определение клиники:** хост из **`LOGTEXT`**, OID и таблицы **`EGISZ_LICENSES`** / **`JPERSONS`** (см. код парсера); цепочка опирается на эти источники.

**2. Идентификация юридического лица (Клиники):**
* **Через URL:** Значение `JID` извлекается из строки хоста в **`EXCHANGELOG.LOGTEXT`** по регулярному выражению `gost-([a-zA-Z0-9_-]+)\.infoclinica\.lan`.
* **Через OID:** Тег `<organization>` из XML-ответа в **`MSGTEXT`** сопоставляется с полем **`EGISZ_LICENSES.MO_UID`** для получения `JID`.
* **Через домен:** Подстрока из `EGISZ_MESSAGES.REPLYTO` сопоставляется с полем `EGISZ_LICENSES.MO_DOMEN` для определения соответствующего `JID`.
* **Наименование:** По полученному `JID` из таблицы **`JPERSONS`** извлекается поле **`JNAME`** (наименование клиники).

**3. Классификация типов документов:**
* Код типа документа (KIND) извлекается из тега `<kind>` в XML-ответе (**`MSGTEXT`**) или из `EGISZ_LICENSES.KIND`.
* Наименование типа документа определяется по справочнику НСИ 1.2.643.5.1.13.13.11.1520.

#### Отчёт «Документы без ответа»
После синхронизации ETL обновляет снимок **`stg_egisz_outbound_documents`** (исходящие сообщения **`EGISZ_MESSAGES`** с непустым **`DOCUMENTID`** за окно **`sync_window_days`**). Представление **`v_rpt_documents_no_response`**: строки снимка, для которых в **`fact_egisz_transactions`** нет **`local_uid_semd`**, совпадающего с **`DOCUMENTID`**. Имена колонок совпадают с **`v_egisz_transactions_enriched`** там, где смысл тот же: **`local_uid_semd`**, **`kind_code`**, **`kind_name`**, **`jid`**, **`clinic_name`**; дополнительно **`gost_host`** (эндпоинт `gost-…infoclinica.lan` или фрагмент **`REPLYTO`**) и **`sent_at`** — момент создания строки в **`EGISZ_MESSAGES`** (в источнике сейчас **`CREATEDATE`** в **`egisz_monitor_corp.sql_util.outbound_documents_staging_select`**). Дашборд Metabase: **`metabase_dashboards/04_documents_no_response.json`**.

### Конфигурация и управление параметрами

Система поддерживает гибридную модель конфигурации, где значения YAML могут перекрываться переменными окружения.

**1. Подключение к Firebird (Источник):**
* **host / port:** `localhost` / `3050`.
* **database:** `proxy_egisz` (алиас или путь к `.fdb`).
* **user / password:** `SYSDBA` / `masterkey`.

**2. Подключение к PostgreSQL (Хранилище DWH):**
* **Локально (Docker Compose):** `localhost:5433`.
* **Kubernetes:** `postgres.egisz-corp.svc.cluster.local:5432`.
* **Учетные данные по умолчанию:**
    * **database / user / password:** `egisz_corp` / `egisz_corp` / `egisz_corp`.

**3. Учетные данные Metabase:**
* **Администрирование (UI и API провижининга):** `admin@egisz.local` / `egisz` (секрет `metabase-admin` в k8s; пример — `k8s/metabase-admin-secret.example.yaml`, при локальном деплое тот же набор пишет `start.ps1` в `k8s/metabase-admin-secret.yaml`).

**4. Управление в Kubernetes (Secrets):**
* **`postgres-credentials`:** Содержит параметры доступа к PostgreSQL.
* **`metabase-admin`:** Email и пароль администратора Metabase (`admin@egisz.local` / `egisz`; пример `k8s/metabase-admin-secret.example.yaml`).
* **`airflow-metadata-connection`:** SQLAlchemy-строка для метаданных Airflow.
* **`egisz-corp-web-config`:** Файл `egisz_corp.yaml` для приложения.

### Инфраструктура

| Сервис | Адрес в K8s | Назначение |
| :--- | :--- | :--- |
| **PostgreSQL** | `postgres:5432` | Основное хранилище витрин данных. |
| **Metabase** | `metabase:3000` | Аналитические дашборды и визуализация. |
| **Config UI** | `conf-ui:8080` | Интерфейс управления YAML-конфигурацией. |

Проверка витрины Postgres и дашбордов Metabase после деплоя: **`.\start.ps1 -Action verify`**. Полный пересоздать namespace и данные: **`.\start.ps1 -Action reset-deploy`**. После **`deploy` / `apply` / `reset-deploy`** по умолчанию поднимается **port-forward** на `http://127.0.0.1:8080/` (Config UI) и `:3000/` (Metabase, без `5432`); отключить: **`-SkipPortForwardAfterDeploy`**. Полный форвард с Postgres: **`.\start.ps1 -Action web`**. В Metabase дашборды — **в корне персональной коллекции** администратора (пункт в сайдбаре «Персональная коллекция …»).

---
*Для получения подробной информации по развертыванию в конкретных окружениях обратитесь к файлу `k8s/README.md`.*