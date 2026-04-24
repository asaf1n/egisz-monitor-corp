"""PostgreSQL warehouse: schema apply, UPSERT fact, dimensions, ETL state, staging errors."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from egisz_monitor_corp.config_loader import PostgresConfig

try:
    import psycopg2
    from psycopg2.extras import Json, execute_batch, execute_values
except ImportError as e:  # pragma: no cover
    raise ImportError("psycopg2-binary is required for ETL.") from e


def connect_pg(cfg: PostgresConfig):  # type: ignore[no-untyped-def]
    con = psycopg2.connect(
        host=cfg.host,
        port=cfg.port,
        dbname=cfg.database,
        user=cfg.user,
        password=cfg.password,
        options=f"-c search_path={cfg.schema}",
    )
    con.autocommit = False
    return con


def sql_dir() -> Path:
    return Path(__file__).resolve().parents[1] / "sql"


def apply_sql_files(con, *names: str) -> None:  # type: ignore[no-untyped-def]
    """Execute bundled .sql files in order (idempotent DDL)."""
    root = sql_dir()
    with con.cursor() as cur:
        for name in names:
            path = root / name
            if not path.is_file():
                raise FileNotFoundError(f"SQL file missing: {path}")
            cur.execute(path.read_text(encoding="utf-8"))
    con.commit()


def ensure_etl_state_table(con) -> None:  # type: ignore[no-untyped-def]
    apply_sql_files(con, "002_etl_state.sql")


def get_last_log_id(con, pipeline: str) -> int:  # type: ignore[no-untyped-def]
    with con.cursor() as cur:
        cur.execute("SELECT last_log_id FROM etl_state WHERE pipeline = %s", (pipeline,))
        row = cur.fetchone()
        return int(row[0]) if row else 0


def set_last_log_id(con, pipeline: str, last_log_id: int) -> None:  # type: ignore[no-untyped-def]
    with con.cursor() as cur:
        cur.execute(
            """
            INSERT INTO etl_state (pipeline, last_log_id, updated_at)
            VALUES (%s, %s, NOW())
            ON CONFLICT (pipeline) DO UPDATE
            SET last_log_id = EXCLUDED.last_log_id, updated_at = NOW();
            """,
            (pipeline, last_log_id),
        )


def upsert_dim_semd(con, kind_code: str, kind_name: str) -> None:  # type: ignore[no-untyped-def]
    with con.cursor() as cur:
        cur.execute(
            """
            INSERT INTO dim_semd_types (kind_code, kind_name)
            VALUES (%s, %s)
            ON CONFLICT (kind_code) DO UPDATE SET kind_name = EXCLUDED.kind_name;
            """,
            (kind_code, kind_name),
        )


def upsert_dim_clinic(con, jid: int, jname: str | None, mo_uid: str | None) -> None:  # type: ignore[no-untyped-def]
    with con.cursor() as cur:
        cur.execute(
            """
            INSERT INTO dim_clinics (jid, jname, mo_uid, updated_at)
            VALUES (%s, %s, %s, NOW())
            ON CONFLICT (jid) DO UPDATE SET
                jname = COALESCE(EXCLUDED.jname, dim_clinics.jname),
                mo_uid = CASE WHEN EXCLUDED.mo_uid <> '' THEN EXCLUDED.mo_uid ELSE dim_clinics.mo_uid END,
                updated_at = NOW();
            """,
            (jid, jname, mo_uid or ""),
        )


def upsert_facts_batch(con, rows: list[dict[str, Any]]) -> None:  # type: ignore[no-untyped-def]
    """Batch UPSERT into fact_egisz_transactions."""
    if not rows:
        return
    tuples: list[tuple[Any, ...]] = []
    for r in rows:
        err = r["errors_json"]
        if isinstance(err, str):
            err = json.loads(err)
        tuples.append(
            (
                r["relates_to_id"],
                r["jid"],
                r["gost_jid_token"],
                r["org_oid"],
                r["kind_code"],
                r["status"],
                r["emdr_id"],
                Json(err),
                r["registration_date"],
                r["processed_at"],
            )
        )
    template = "(%s, %s, %s, %s, %s, %s, %s, %s::jsonb, %s, %s)"  # Json() → jsonb
    with con.cursor() as cur:
        execute_values(
            cur,
            """
            INSERT INTO fact_egisz_transactions (
                relates_to_id, jid, gost_jid_token, org_oid, kind_code, status,
                emdr_id, errors_json, registration_date, processed_at
            ) VALUES %s
            ON CONFLICT (relates_to_id) DO UPDATE SET
                jid = EXCLUDED.jid,
                gost_jid_token = EXCLUDED.gost_jid_token,
                org_oid = EXCLUDED.org_oid,
                kind_code = EXCLUDED.kind_code,
                status = EXCLUDED.status,
                emdr_id = EXCLUDED.emdr_id,
                errors_json = EXCLUDED.errors_json,
                registration_date = EXCLUDED.registration_date,
                processed_at = EXCLUDED.processed_at
            """,
            tuples,
            template=template,
        )


def insert_staging_errors(con, rows: list[tuple[str | None, str, str, str | None]]) -> None:  # type: ignore[no-untyped-def]
    if not rows:
        return
    with con.cursor() as cur:
        execute_batch(
            cur,
            """
            INSERT INTO stg_parse_errors (relates_to_id, error_code, message, log_excerpt)
            VALUES (%s, %s, %s, %s);
            """,
            rows,
        )


def test_pg_connection(cfg: PostgresConfig) -> None:  # type: ignore[no-untyped-def]
    con = connect_pg(cfg)
    try:
        with con.cursor() as cur:
            cur.execute("SELECT 1")
            cur.fetchone()
    finally:
        con.close()
