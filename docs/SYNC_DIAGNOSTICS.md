# Диагностика: `sync_window_days` и полнота данных

Практическая сверка конфигурации ETL и сопоставление объёмов **Firebird** и **PostgreSQL** по тем же предикатам, что в коде (`sql_util`, `etl.run_sync`).

## 1. Актуальные параметры ETL (verify-config)

| Параметр | Где смотреть | Назначение |
|----------|--------------|------------|
| `etl.sync_window_days` | [`config/egisz_corp.yaml`](../config/egisz_corp.yaml), k8s ConfigMap, Config UI; приоритет: **`EGISZ_CORP_CONFIG`** | Окно по **`EXCHANGELOG.LOGDATE`** и по **`EGISZ_MESSAGES.CREATEDATE`** для staging исходящих. |
| `etl.full_scan` | то же | **`true`**: курсор **`last_log_id`** в начале прогона сбрасывается в 0; перечитывается журнал **в рамках окна** по `LOGDATE`. |
| `etl.batch_size` | то же | Размер страницы **по `LOGID`**; **не** меняет календарное окно. |
| `etl.pipeline_name` | то же (по умолчанию `firebird_exchangelog`) | Ключ строки в **`etl_state`**. |

**Проверка:** убедитесь, что смотрите тот же файл, что использует запуск (`kubectl exec`, Airflow, CLI с `--config`). После правки YAML перезапуск процесса не всегда обязателен для следующего ручного `sync`, но переменная окружения должна указывать на актуальный путь.

## 2. Курсор в PostgreSQL

Выполните в витрине (имя пайплайна замените при необходимости):

```sql
SELECT pipeline, last_log_id, updated_at
FROM etl_state
WHERE pipeline = 'firebird_exchangelog';
```

Либо см. готовый блок в [`sql/004_diagnostic_counts_postgres.sql`](../sql/004_diagnostic_counts_postgres.sql).

## 3. Сверка счётчиков (compare-counts)

Используйте параметр **`N`** = текущее **`etl.sync_window_days`** из конфигурации.

### Журнал `EXCHANGELOG`

- В Firebird: запросы из [`sql/003_diagnostic_counts_firebird.sql`](../sql/003_diagnostic_counts_firebird.sql) — общее число строк в окне по `LOGDATE` и число строк **после курсора** (`LOGID > last_log_id` из шага 2).
- В логах последнего **`egisz-corp sync`** или UI: **`fetched`**, **`facts_upserted`** (не равны числу строк журнала: часть строк не даёт факт, тестовые клиники отфильтрованы, ошибки в **`stg_parse_errors`**).

**Интерпретация:** если COUNT в Firebird (с окном и курсором) существенно больше, чем ожидаемые обработанные строки, смотрите **`stg_parse_errors`**, фильтр тестовых МО и полноту парсинга.

### Исходящие (`stg_egisz_outbound_documents`)

- Каждый успешный sync **удаляет** все строки staging и вставляет снимок за окно по **`CREATEDATE`** в Firebird (см. `pg_warehouse.refresh_outbound_documents_staging`).
- Сравните COUNT во Firebird по условию из файла `003` с **`SELECT COUNT(*) FROM stg_egisz_outbound_documents`** в PG. Учтите: ETL оставляет **одну строку на `DOCUMENTID`** и отбрасывает тестовые клиники по имени — расхождение на эти величины нормально.

### Факты `fact_egisz_transactions`

Таблица **не очищается** по окну: только UPSERT. Старые факты могут оставаться при уменьшении **`sync_window_days`**. Сравнение «всего фактов» с COUNT журнала во Firebird по окну **напрямую** некорректно без ограничения фактов по датам в PG.

## 4. Файлы SQL в репозитории

| Файл | База |
|------|------|
| [`sql/003_diagnostic_counts_firebird.sql`](../sql/003_diagnostic_counts_firebird.sql) | Firebird |
| [`sql/004_diagnostic_counts_postgres.sql`](../sql/004_diagnostic_counts_postgres.sql) | PostgreSQL |

Подставьте **`N`** и при необходимости **`last_log_id`** из **`etl_state`** перед выполнением.

## 5. Логи ETL

После синхронизации в stdout / UI доступна строка вида:

```text
fetched=… facts=… staging_errors=… max_log_id=… cursor_after=…
```

Сопоставляйте **`fetched`** с Firebird COUNT журнала по окну и курсору; **`facts`** — с числом успешно построенных фактов (≤ **`fetched`** при типичных данных).
