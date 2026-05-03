"""CLI: ETL sync, test connections, apply SQL."""

from __future__ import annotations

import argparse
import os
import sys

from egisz_monitor_corp.config_loader import load_corp_config
from egisz_monitor_corp.etl import run_sync
from egisz_monitor_corp.fb_client import fetch_all
from egisz_monitor_corp.k8s_cronjob import reconcile_egisz_monitor_sync_cronjob
from egisz_monitor_corp.pg_warehouse import (
    PipelineLockBusyError,
    apply_reports_schema,
    connect_pg,
    test_pg_connection,
)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="egisz-monitor")
    p.add_argument("--config", type=str, default=None, help="Path to egisz_monitor.yaml (or EGISZ_MONITOR_CONFIG)")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("sync", help="Run Firebird -> Postgres ETL")
    s.add_argument("--dry-run", action="store_true", help="Parse only, no Postgres writes")

    t = sub.add_parser("test-fb", help="SELECT 1 FROM RDB$DATABASE on Firebird")
    t2 = sub.add_parser("test-pg", help="SELECT 1 on PostgreSQL")

    a = sub.add_parser(
        "apply-schema",
        help="Применить DDL витрины (файлы из sql/schema_apply_order.txt, по умолчанию 001+002+005)",
    )

    sub.add_parser("config-ui", help="Run Flask config editor on FLASK_RUN_HOST:FLASK_RUN_PORT")

    kc = sub.add_parser(
        "k8s-reconcile-cronjob",
        help="Выставить CronJob egisz-monitor-sync по auto_sync из YAML (suspend/schedule/timeZone)",
    )
    kc.add_argument("--namespace", default=None, help="Namespace (по умолчанию egisz-monitor или из SA)")
    kc.add_argument("--cronjob-name", default=None, help="Имя CronJob (по умолчанию egisz-monitor-sync)")

    args = p.parse_args(argv)
    if args.config:
        os.environ["EGISZ_MONITOR_CONFIG"] = args.config

    if args.cmd == "sync":
        cfg = load_corp_config()
        try:
            stats = run_sync(
                cfg, dry_run=args.dry_run, progress_cb=lambda m: print(m, file=sys.stderr)
            )
        except PipelineLockBusyError as e:
            # CronJob и UI-кнопка делят один advisory lock в Postgres. Если параллельно стартанул
            # другой процесс — выходим с кодом 75 (EX_TEMPFAIL), Kubernetes Job увидит it as Failed
            # и отразит причину в логах, не повторяя попытку до следующего расписания.
            print(f"sync_skipped: {e}", file=sys.stderr)
            return 75
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
            apply_reports_schema(pg)
        finally:
            pg.close()
        print("schema_applied")
        return 0

    if args.cmd == "config-ui":
        from egisz_monitor_corp.config_app import run_dev

        run_dev()
        return 0

    if args.cmd == "k8s-reconcile-cronjob":
        cfg = load_corp_config()
        raw = {"enabled": cfg.auto_sync.enabled, "schedule_cron": cfg.auto_sync.schedule_cron, "timezone": cfg.auto_sync.timezone}
        ok, detail = reconcile_egisz_monitor_sync_cronjob(
            raw,
            namespace=args.namespace,
            cronjob_name=args.cronjob_name,
        )
        print(detail)
        return 0 if ok else 1

    return 1


def main_entry() -> None:
    raise SystemExit(main())


if __name__ == "__main__":
    main_entry()
