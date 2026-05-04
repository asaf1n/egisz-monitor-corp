"""
Airflow DAG: EGISZ Monitor — Firebird EXCHANGELOG → PostgreSQL.

Install package in Airflow image / venv:
  pip install -e /opt/egisz-monitor-corp

Config (YAML), default path inside project:
  /opt/egisz-monitor-corp/config/egisz_monitor.yaml
Override with env EGISZ_MONITOR_CONFIG.

Requires Firebird client library on workers for firebird-driver (FB_CLIENT_LIBRARY).
"""

from __future__ import annotations

import os
import sys
from datetime import timedelta

from airflow import DAG
from airflow.models import Variable
from airflow.operators.python import PythonOperator

DEFAULT_PROJECT_ROOT = os.environ.get("EGISZ_MONITOR_PROJECT_ROOT", "/opt/egisz-monitor-corp")


def _monitor_sync(**_context) -> str:
    root = Variable.get("egisz_monitor_project_root", default_var=DEFAULT_PROJECT_ROOT)
    cfg_path = Variable.get(
        "egisz_monitor_config_path", default_var=os.path.join(root, "config", "egisz_monitor.yaml")
    )
    if root not in sys.path:
        sys.path.insert(0, root)

    os.environ["EGISZ_MONITOR_CONFIG"] = cfg_path

    from egisz_monitor_corp.config_loader import load_corp_config
    from egisz_monitor_corp.etl import run_sync
    from egisz_monitor_corp.pg_warehouse import (
        PipelineLockBusyError,
        connect_pg,
        fetch_etl_watermark_row,
    )

    cfg = load_corp_config()
    pipe = cfg.etl.pipeline_name
    try:
        pg0 = connect_pg(cfg.postgres)
        try:
            wm = fetch_etl_watermark_row(pg0, pipe)
        finally:
            pg0.close()
        if wm:
            msg_snap = wm.get("messages_snapshot_high_egmid", 0)
            print(
                "etl_watermarks_before_sync "
                f"pipeline={pipe} last_log_id={wm['last_log_id']} "
                f"last_egmid={wm['last_egmid']} source_max_egmid={wm['source_max_egmid']} "
                f"messages_snapshot_high_egmid={msg_snap}",
                flush=True,
            )
        else:
            print(f"etl_watermarks_before_sync pipeline={pipe} etl_state_row=missing", flush=True)
    except Exception as e:
        print(f"etl_watermarks_before_sync pipeline={pipe} error={e!s}", flush=True)

    try:
        stats = run_sync(cfg, progress_cb=lambda m: print(m, flush=True))
    except PipelineLockBusyError as e:
        print(f"monitor_sync_skipped: {e}", flush=True)
        return f"skipped_lock: {e!s}"

    return (
        f"fetched={stats.fetched} facts={stats.facts_upserted} staging_errors={stats.staging_errors} "
        f"max_log_id={stats.max_log_id} cursor={stats.last_cursor_after}"
    )


def _test_connections(**_context) -> str:
    root = Variable.get("egisz_monitor_project_root", default_var=DEFAULT_PROJECT_ROOT)
    cfg_path = Variable.get(
        "egisz_monitor_config_path", default_var=os.path.join(root, "config", "egisz_monitor.yaml")
    )
    if root not in sys.path:
        sys.path.insert(0, root)
    os.environ["EGISZ_MONITOR_CONFIG"] = cfg_path

    from egisz_monitor_corp.config_loader import load_corp_config
    from egisz_monitor_corp.fb_client import fetch_all
    from egisz_monitor_corp.pg_warehouse import test_pg_connection

    cfg = load_corp_config()
    fetch_all(cfg.firebird, "SELECT 1 AS OK FROM RDB$DATABASE")
    test_pg_connection(cfg.postgres)
    return "firebird_ok postgres_ok"


with DAG(
    dag_id="egisz_monitor_firebird_to_postgres",
    description="Monitor ETL: MSGTEXT SOAP + LOGTEXT host → fact_egisz_transactions (LOGID watermark)",
    schedule_interval=os.environ.get("EGISZ_MONITOR_AIRFLOW_SCHEDULE", "@hourly"),
    start_date=__import__("datetime").datetime(2026, 1, 1),
    catchup=False,
    tags=["egisz", "firebird", "postgres", "monitor"],
    default_args={
        "owner": "data-engineering",
        "retries": 1,
        "retry_delay": timedelta(minutes=5),
    },
) as dag:
    test = PythonOperator(task_id="test_connections", python_callable=_test_connections)
    sync = PythonOperator(task_id="monitor_sync", python_callable=_monitor_sync)
    test >> sync
