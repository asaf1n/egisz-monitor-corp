# Power BI рядом с Metabase (EGISZ Corp DWH)

Metabase остаётся основным развёрнутым BI в кластере; Power BI — **отдельный клиентский** инструмент поверх **той же** витрины PostgreSQL. Схему БД и ETL менять не нужно.

## Лицензии (кратко)

| Вариант | Назначение |
|--------|------------|
| **Power BI Desktop** | Бесплатно: моделирование, отчёты, подключение к PostgreSQL (Import / DirectQuery). Актуальные ограничения по размеру датасета и публикации — на [странице цен Power BI](https://www.microsoft.com/power-platform/products/power-bi/pricing). |
| **Облако, лицензия Free** | Личная работа в My Workspace; командная публикация и общие рабочие области обычно требуют **Pro** или ёмкости **Premium / Fabric**. |
| **On-premises data gateway** | Для облачного обновления данных из Postgres за периметром; ставится на Windows, не как под рядом с Metabase. |

## Подключение к PostgreSQL

1. Установите [Power BI Desktop](https://powerbi.microsoft.com/desktop/).
2. При необходимости установите драйвер PostgreSQL, который предлагает мастер подключения (часто **Npgsql**).
3. **Получить данные** → **База данных** → **База данных PostgreSQL**.
4. Параметры (как у Metabase, см. [METABASE.md](METABASE.md) и секреты k8s):

| Сценарий | Сервер | Порт | База |
|----------|--------|------|------|
| Docker Compose (корень репозитория) | `localhost` | `5433` (или `CORP_DB_PORT` из `.env`) | из `POSTGRES_DB`, по умолчанию `egisz_reports` |
| Kubernetes из ПК | `localhost` | `5432` после `kubectl port-forward` / `.\start.ps1 -Action web` | из `postgres-credentials` |
| Внутри кластера | `postgres` или `postgres.egisz-corp.svc.cluster.local` | `5432` | из секрета |

5. Режим загрузки: для больших объёмов рассмотрите **DirectQuery** к представлениям `*_ui`; для небольших витрин — **Import** с расписанием обновления (в облаке — через шлюз).

## Объекты витрины (как в Metabase)

Полный список и комментарии по кириллице — в [METABASE.md](METABASE.md). Для отчётов с русскими именами колонок используйте:

- `public.v_egisz_transactions_enriched_ui` — факты и обогащение (основная витрина).
- `public.v_rpt_documents_no_response_ui` — документы без ответа.
- `public.stg_parse_errors` — ошибки разбора XML / отсутствие `relatesToMessage`.
- `public.etl_state` — курсор ETL (`last_log_id`), **не** бизнес-время транзакций.

## SQL для карточек Metabase

Тексты **native SQL** из провижининга Metabase вынесены в каталог [../powerbi/egisz-corp/sql/](../powerbi/egisz-corp/sql/) по номерам дашбордов (01–09) и дополнительные запросы (10–13). Их можно вставить в Power Query: **Преобразовать данные** → правый клик по запросу → **Дополнительный редактор** → источник **PostgreSQL** с **Дополнительными параметрами** → **SQL-инструкция** (или эквивалент в вашей локали).

Чтобы обновить SQL из репозитория Metabase после правок JSON:

```powershell
# пример: найти все native query в metabase_dashboards
Select-String -Path "metabase_dashboards\*.json" -Pattern '"query":'
```

## Формат проекта (PBIP)

Каталог [../powerbi/egisz-corp/](../powerbi/egisz-corp/) содержит **SQL-источники** и заготовку **Power BI Project** (`.pbip` + артефакты). Подробности открытия и доработки модели под ваш хост/пароль — в [../powerbi/egisz-corp/README.md](../powerbi/egisz-corp/README.md).

Если кириллица в подписях отображается некорректно, следуйте разделу «Кодировка кириллицы» в [METABASE.md](METABASE.md).

## Соответствие страниц Metabase и файлов SQL

| № | Metabase (JSON) | Файл SQL в репозитории |
|---|-----------------|-------------------------|
| 01 | `metabase_dashboards/01_operational.json` | `sql/01_operational.sql` |
| 02 | `02_service.json` | `sql/02_service.sql` |
| 03 | `03_errors.json` | `sql/03_errors.sql` |
| 04 | `04_documents_no_response.json` | `sql/04_documents_no_response.sql` |
| 05 | `05_trends.json` | `sql/05_trends.sql` |
| 06 | `06_quality.json` | `sql/06_quality.sql` |
| 07 | `07_errors_deep.json` | `sql/07_errors_deep.sql` |
| 08 | `08_pending_agg.json` | `sql/08_pending_agg.sql` |
| 09 | `09_executive.json` | `sql/09_executive.sql` |
| 10–13 | Доп. цели сервиса | `sql/10_document_trace.sql` … `sql/13_etl_health.sql` |
