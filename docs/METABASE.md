# Metabase на корпоративном DWH

## Подключение

1. В Metabase: **Admin → Databases → Add database → PostgreSQL**.
2. Используйте те же параметры, что в `config/egisz_corp.yaml` → секция `postgres`.
3. Схема по умолчанию: `public` (или значение `postgres.schema` в YAML).

## Кодировка кириллицы (клиники, подписи на графиках)

Если в Metabase на осях или в таблице вместо русских букв отображаются знаки вопроса (`????`):

1. **Firebird:** в `egisz_corp.yaml` задайте корректный `firebird.charset` для вашей БД. Для классических русскоязычных баз Infoclinica часто подходит **`WIN1251`** (значение по умолчанию в `egisz_corp.example.yaml` и в `k8s/local/egisz_corp.yaml`). После смены charset выполните полный цикл ETL, чтобы перезаписать `dim_clinics.jname` и связанные витрины.
2. **Контейнер Metabase:** образ задаёт `LANG`/`LC_ALL` в `C.UTF-8`, чтобы скрипты провижининга и JDBC не ломали UTF-8 при передаче нативных SQL из `metabase_dashboards/`.

## Объекты для дашбордов

После `egisz-corp apply-schema` и успешного `egisz-corp sync`:

| Объект | Назначение |
|--------|------------|
| `v_egisz_transactions_enriched` | Техническая витрина (snake_case): факты + СЭМД + клиника; **не переименовывать колонки здесь** — от этого зависят другие представления и ETL. |
| `v_egisz_transactions_enriched_ui` | То же содержимое, колонки с **русскими подписями** для Metabase (имена как в `dim_column_display_labels`). |
| `v_rpt_documents_no_response` | Очередь «документы без ответа» (технические имена колонок). |
| `v_rpt_documents_no_response_ui` | То же для отчётов с русскими именами колонок. |
| `dim_column_display_labels` | Справочник сопоставления `source_object` + `source_column` → `display_label_ru` (синхронизирован с колонками `*_ui`). |
| `fact_egisz_transactions` | Сырой факт (JSON ошибок, статусы, `local_uid_semd`) |
| `stg_parse_errors` | Строки без `relatesToMessage` / битый XML в **MSGTEXT** |
| `dim_clinics` | `jname`, `jinn`, `fir_oid`, `mo_uid` (ETL: `JPERSONS` + `EGISZ_LICENSES` по `JID`) |
| `dim_semd_types` | KIND → наименование |
| `etl_state` | Контроль курсора `last_log_id` (не используйте как бизнес-время) |

Карточки в `metabase_dashboards/` по умолчанию читают **`v_egisz_transactions_enriched_ui`** и **`v_rpt_documents_no_response_ui`**, чтобы заголовки таблиц и осей совпадали с подписями без ручного дублирования в каждом запросе.

## Пример вопроса (SQL)

```sql
SELECT "Статус", COUNT(*)::bigint AS "Количество"
FROM v_egisz_transactions_enriched_ui
WHERE "Обработано" > NOW() - INTERVAL '7 days'
GROUP BY 1;
```

## Деплой и обновление карточек (GitHub Actions / CI)

JSON из `metabase_dashboards/` попадают в Metabase **только** когда в поде выполняется **`/app/setup-dashboards.sh`** (его вызывает **`metabase/provision.sh`** при старте контейнера Metabase).

Чтобы после merge в `main` изменения дашбордов реально применились:

1. **Соберите и выкатите образ Metabase**, а не только приложение ETL: `docker build -f metabase/Dockerfile -t …` (в репозитории цель `egisz-corp-metabase`, см. `start.ps1` / `k8s/metabase.yaml`). В образ копируется каталог `metabase_dashboards/`.
2. **Перезапустите Deployment Metabase**, чтобы снова выполнился `entrypoint` → `provision.sh` → `setup-dashboards.sh` (пересоздание коллекций и карточек из JSON).
3. Убедитесь, что в момент старта пода в Postgres уже есть таблицы витрины (`apply-schema` / Job). Иначе раньше провижининг **один раз** пропускался и больше не повторялся — в `provision.sh` добавлено **ожидание схемы** (до ~10 минут) перед `setup-dashboards.sh`.

Локально: `docker build -f metabase/Dockerfile -t egisz-corp-metabase:latest .` и перезапуск контейнера Metabase.

## Переменные Airflow (опционально)

- `egisz_corp_project_root` — корень репозитория с пакетом (для `sys.path`).
- `egisz_corp_config_path` — абсолютный путь к `egisz_corp.yaml`.

Расписание DAG: env `EGISZ_CORP_AIRFLOW_SCHEDULE` (cron или макрос Airflow, по умолчанию `@hourly`).
