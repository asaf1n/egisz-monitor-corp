# egisz-monitor-corp

Корпоративное ядро ETL для мониторинга интеграции МИС с ЕГИСЗ (РЭМД): выгрузка из Firebird (`EXCHANGELOG` + `EGISZ_MESSAGES` + поля из `EGISZ_LICENSES`), разбор `LOGTEXT` (SOAP + транспорт), загрузка в PostgreSQL для Metabase.

## Отличия от `egisz-monitor`

- Только Python (без React и без TypeScript ETL).
- Ключ витрины: `relates_to_id` ← `relatesToMessage`, UPSERT в `fact_egisz_transactions`.
- Водяной знак инкремента: **`LOGID` в `etl_state`**, не `MODIFYDATE` (источник перезаписывается).
- **KIND** только в `EGISZ_LICENSES` (см. дефолтный SQL в `egisz_monitor_corp/sql_util.py`).

## Установка

```bash
cd egisz-monitor-corp
pip install -e ".[dev]"
python3 -m pytest
```

Требования к окружению: для `firebird-driver` на машине должен быть доступен **Firebird client** (`FB_CLIENT_LIBRARY` или библиотека в `PATH`).

## Среда из репозитория (`start.ps1`, Windows)

Скрипт `start.ps1 -Action deploy` поднимает **только** витрину PostgreSQL из `docker-compose.yml` этого каталога (удобный dev на одной машине), при необходимости создаёт `.env` и `config/egisz_corp.yaml`, ставит **venv** и выполняет **`egisz-corp apply-schema`**. В конце выводится **справка** по сервисам и URL.

**Продакшен:** витрина **PostgreSQL** и **Apache Airflow** размещаются в **Kubernetes** — см. **`k8s/README.md`** (Helm, Job для БД `airflow`, образ с DAG, секреты). **Metabase** в compose и в этом Helm-пакете не входит; подключайте к тому же Postgres в k8s (см. `docs/METABASE.md`). Синхронизация с Firebird — **`egisz-corp sync`** из venv или DAG в Airflow, не кнопка во Flask UI.

```powershell
cd egisz-monitor-corp
.\start.ps1              # deploy (по умолчанию)
.\start.ps1 -Action down
.\start.ps1 -Action help
```

Порт Postgres на хосте по умолчанию **5433** (см. `CORP_DB_PORT` в `.env.example`), чтобы не пересечься с основным стеком на 5432. Дефолтный Firebird в `config/egisz_corp.example.yaml`: `localhost`, alias `proxy_egisz`, `SYSDBA` / `masterkey`; из контейнера/Kubernetes к хосту Windows используйте **`host.docker.internal`**.

## Конфигурация

1. Скопируйте `config/egisz_corp.example.yaml` → `config/egisz_corp.yaml`.
2. Либо задайте `EGISZ_CORP_CONFIG=/abs/path/egisz_corp.yaml`.

### Веб-страница настроек (Flask)

Сервер нужно **запустить** (пока процесс работает, страница открывается; иначе браузер покажет `ERR_CONNECTION_REFUSED`).

```powershell
cd egisz-monitor-corp
.\start.ps1 -Action ui
```

Либо из активированного venv:

```bash
export EGISZ_CORP_CONFIG=/path/to/egisz_corp.yaml   # опционально
export CONFIG_WRITE_PATH=/path/to/egisz_corp.yaml   # куда писать при «Сохранить» (по умолчанию = EGISZ_CORP_CONFIG / config/egisz_corp.yaml)
egisz-corp config-ui
# http://127.0.0.1:8765/  — хост/порт через FLASK_RUN_HOST / FLASK_RUN_PORT
```

## CLI

```bash
egisz-corp test-fb
egisz-corp test-pg
egisz-corp apply-schema      # 001_schema + 002_etl_state
egisz-corp sync              # полный цикл ETL
egisz-corp sync --dry-run    # только разбор, без записи в PG
```

## Airflow

DAG: `airflow/dags/egisz_corp_etl_dag.py` — задачи `test_connections` → `corp_sync`.

- **Kubernetes:** развёртывание официальным Helm chart, образ с пакетом и DAG — см. **`k8s/README.md`**, **`k8s/airflow/Dockerfile`**, **`k8s/airflow/values-corp.example.yaml`**.
- Переменные Airflow (опционально): `egisz_corp_project_root`, `egisz_corp_config_path`.
- Расписание: env `EGISZ_CORP_AIRFLOW_SCHEDULE` (по умолчанию `@hourly`).

## Metabase

См. `docs/METABASE.md`: подключение к тому же PostgreSQL, объекты `v_egisz_transactions_enriched`, `stg_parse_errors`, `etl_state`.

## Kubernetes (Postgres + Airflow)

См. **`k8s/README.md`**: namespace `egisz-corp`, Postgres (`k8s/postgres/`), Job создания БД `airflow`, секрет метаданных Airflow, Helm. Секрет Postgres — из `k8s/postgres/postgres-secret.example.yaml` в файл вроде `postgres-credentials.yaml` (см. `.gitignore`).

## Схема БД

- `sql/001_schema.sql` — факты, измерения, витрина.
- `sql/002_etl_state.sql` — курсор `last_log_id`.

## API парсера

Класс `EgiszMonitorParser` (`egisz_monitor_corp/parser.py`):

- `parse_xml`, `extract_jid`, `resolve_clinic`, `build_record` — см. docstring в модуле.
