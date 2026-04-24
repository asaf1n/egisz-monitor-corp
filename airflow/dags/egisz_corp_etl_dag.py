"""
Airflow DAG: EGISZ Monitor Corp — Firebird EXCHANGELOG → PostgreSQL.

Install package in Airflow image / venv:
  pip install -e /opt/egisz-monitor-corp

Config (YAML), default path inside project:
  /opt/egisz-monitor-corp/config/egisz_corp.yaml
Override with env EGISZ_CORP_CONFIG.

Requires Firebird client library on workers for firebird-driver (FB_CLIENT_LIBRARY).
"""

from __future__ import annotations

import os
import sys
from datetime import timedelta

from airflow import DAG
from airflow.models import Variable
from airflow.operators.python import PythonOperator

DEFAULT_PROJECT_ROOT = os.environ.get("EGISZ_CORP_PROJECT_ROOT", "/opt/egisz-monitor-corp")


def _corp_sync(**_context) -> str:
    root = Variable.get("egisz_corp_project_root", default_var=DEFAULT_PROJECT_ROOT)
    cfg_path = Variable.get("egisz_corp_config_path", default_var=os.path.join(root, "config", "egisz_corp.yaml"))
    if root not in sys.path:
        sys.path.insert(0, root)

    os.environ["EGISZ_CORP_CONFIG"] = cfg_path

    from egisz_monitor_corp.etl import run_sync
    from egisz_monitor_corp.config_loader import load_corp_config

    cfg = load_corp_config()
    stats = run_sync(cfg, progress_cb=lambda m: print(m, flush=True))
    return (
        f"fetched={stats.fetched} facts={stats.facts_upserted} staging_errors={stats.staging_errors} "
        f"max_log_id={stats.max_log_id} cursor={stats.last_cursor_after}"
    )


def _test_connections(**_context) -> str:
    root = Variable.get("egisz_corp_project_root", default_var=DEFAULT_PROJECT_ROOT)
    cfg_path = Variable.get("egisz_corp_config_path", default_var=os.path.join(root, "config", "egisz_corp.yaml"))
    if root not in sys.path:
        sys.path.insert(0, root)
    os.environ["EGISZ_CORP_CONFIG"] = cfg_path

    from egisz_monitor_corp.config_loader import load_corp_config
    from egisz_monitor_corp.fb_client import fetch_all
    from egisz_monitor_corp.pg_warehouse import test_pg_connection

    cfg = load_corp_config()
    fetch_all(cfg.firebird, "SELECT 1 AS OK FROM RDB$DATABASE")
    test_pg_connection(cfg.postgres)
    return "firebird_ok postgres_ok"


with DAG(
    dag_id="egisz_corp_firebird_to_postgres",
    description="Corp ETL: LOGTEXT parse → fact_egisz_transactions (LOGID watermark)",
    schedule_interval=os.environ.get("EGISZ_CORP_AIRFLOW_SCHEDULE", "@hourly"),
    start_date=__import__("datetime").datetime(2026, 1, 1),
    catchup=False,
    tags=["egisz", "firebird", "postgres", "corp"],
    default_args={
        "owner": "data-engineering",
        "retries": 1,
        "retry_delay": timedelta(minutes=5),
    },
) as dag:
    test = PythonOperator(task_id="test_connections", python_callable=_test_connections)
    sync = PythonOperator(task_id="corp_sync", python_callable=_corp_sync)
    test >> sync
