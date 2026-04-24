"""CLI: ETL sync, test connections, apply SQL."""

from __future__ import annotations

import argparse
import os
import sys

from egisz_monitor_corp.config_loader import load_corp_config
from egisz_monitor_corp.etl import run_sync
from egisz_monitor_corp.fb_client import fetch_all
from egisz_monitor_corp.pg_warehouse import apply_sql_files, connect_pg, ensure_etl_state_table, test_pg_connection


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="egisz-corp")
    p.add_argument("--config", type=str, default=None, help="Path to egisz_corp.yaml (or EGISZ_CORP_CONFIG)")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("sync", help="Run Firebird -> Postgres ETL")
    s.add_argument("--dry-run", action="store_true", help="Parse only, no Postgres writes")

    t = sub.add_parser("test-fb", help="SELECT 1 FROM RDB$DATABASE on Firebird")
    t2 = sub.add_parser("test-pg", help="SELECT 1 on PostgreSQL")

    a = sub.add_parser("apply-schema", help="Apply sql/001_schema.sql and 002_etl_state.sql")

    sub.add_parser("config-ui", help="Run Flask config editor on FLASK_RUN_HOST:FLASK_RUN_PORT")

    args = p.parse_args(argv)
    if args.config:
        os.environ["EGISZ_CORP_CONFIG"] = args.config

    if args.cmd == "sync":
        cfg = load_corp_config()
        stats = run_sync(cfg, dry_run=args.dry_run, progress_cb=lambda m: print(m, file=sys.stderr))
        print(
            f"fetched={stats.fetched} facts={stats.facts_upserted} staging_errors={stats.staging_errors} "
            f"max_log_id={stats.max_log_id} cursor_after={stats.last_cursor_after}"
        )
        return 0

    if args.cmd == "test-fb":
        cfg = load_corp_config()
        fetch_all(cfg.firebird, "SELECT 1 AS OK FROM RDB$DATABASE")
        print("firebird_ok")
        return 0

    if args.cmd == "test-pg":
        cfg = load_corp_config()
        test_pg_connection(cfg.postgres)
        print("postgres_ok")
        return 0

    if args.cmd == "apply-schema":
        cfg = load_corp_config()
        pg = connect_pg(cfg.postgres)
        try:
            apply_sql_files(pg, "001_schema.sql", "002_etl_state.sql")
            ensure_etl_state_table(pg)
        finally:
            pg.close()
        print("schema_applied")
        return 0

    if args.cmd == "config-ui":
        from egisz_monitor_corp.config_app import run_dev

        run_dev()
        return 0

    return 1


def main_entry() -> None:
    raise SystemExit(main())


if __name__ == "__main__":
    main_entry()
