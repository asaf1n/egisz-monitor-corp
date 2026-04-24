# Metabase на корпоративном DWH

## Подключение

1. В Metabase: **Admin → Databases → Add database → PostgreSQL**.
2. Используйте те же параметры, что в `config/egisz_corp.yaml` → секция `postgres`.
3. Схема по умолчанию: `public` (или значение `postgres.schema` в YAML).

## Объекты для дашбордов

После `egisz-corp apply-schema` и успешного `egisz-corp sync`:

| Объект | Назначение |
|--------|------------|
| `v_egisz_transactions_enriched` | Факты + название СЭМД + имя клиники |
| `fact_egisz_transactions` | Сырой факт (JSON ошибок, статусы) |
| `stg_parse_errors` | Строки без `relatesToMessage` / битый XML |
| `dim_clinics` | JID, JNAME, MO_UID |
| `dim_semd_types` | KIND → наименование |
| `etl_state` | Контроль курсора `last_log_id` (не используйте как бизнес-время) |

## Пример вопроса (SQL)

```sql
SELECT status, COUNT(*) AS n
FROM v_egisz_transactions_enriched
WHERE processed_at > NOW() - INTERVAL '7 days'
GROUP BY 1;
```

## Переменные Airflow (опционально)

- `egisz_corp_project_root` — корень репозитория с пакетом (для `sys.path`).
- `egisz_corp_config_path` — абсолютный путь к `egisz_corp.yaml`.

Расписание DAG: env `EGISZ_CORP_AIRFLOW_SCHEDULE` (cron или макрос Airflow, по умолчанию `@hourly`).
