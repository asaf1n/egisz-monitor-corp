"""PostgreSQL warehouse: schema apply, UPSERT fact, dimensions, ETL state, staging errors."""

from __future__ import annotations

import json
import os
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
    con.set_client_encoding("UTF8")
    con.autocommit = False
    return con


def sql_dir() -> Path:
    """Репозиторий: <root>/sql. Wheel в контейнере: задайте EGISZ_CORP_SQL_DIR (см. docker/web/Dockerfile)."""
    override = (os.environ.get("EGISZ_CORP_SQL_DIR") or "").strip()
    if override:
        return Path(override)
    return Path(__file__).resolve().parent.parent / "sql"


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


def upsert_dim_clinic(
    con,
    jid: int,
    jname: str | None,
    mo_uid: str | None,
    *,
    jinn: str | None = None,
    fir_oid: str | None = None,
) -> None:  # type: ignore[no-untyped-def]
    jin = (jinn or "").strip()
    fir = (fir_oid or "").strip()
    with con.cursor() as cur:
        cur.execute(
            """
            INSERT INTO dim_clinics (jid, jname, mo_uid, jinn, fir_oid, updated_at)
            VALUES (%s, %s, %s, %s, %s, NOW())
            ON CONFLICT (jid) DO UPDATE SET
                jname = COALESCE(EXCLUDED.jname, dim_clinics.jname),
                mo_uid = CASE WHEN EXCLUDED.mo_uid <> '' THEN EXCLUDED.mo_uid ELSE dim_clinics.mo_uid END,
                jinn = CASE WHEN EXCLUDED.jinn <> '' THEN EXCLUDED.jinn ELSE dim_clinics.jinn END,
                fir_oid = CASE WHEN EXCLUDED.fir_oid <> '' THEN EXCLUDED.fir_oid ELSE dim_clinics.fir_oid END,
                updated_at = NOW();
            """,
            (jid, jname, mo_uid or "", jin, fir),
        )


def refresh_outbound_documents_staging(con, rows: list[dict[str, Any]]) -> None:  # type: ignore[no-untyped-def]
    """Полная перезапись stg_egisz_outbound_documents снимком за окно (см. outbound_documents_staging_select)."""
    with con.cursor() as cur:
        cur.execute("DELETE FROM stg_egisz_outbound_documents")
    if not rows:
        return
    tuples: list[tuple[Any, ...]] = []
    for r in rows:
        tuples.append(
            (
                r["document_id"],
                r.get("sent_at"),
                r.get("reply_to"),
                r.get("gost_jid_token"),
                r.get("kind_code"),
                r.get("jid"),
                r.get("egmid"),
            )
        )
    template = "(%s, %s, %s, %s, %s, %s, %s, NOW())"
    with con.cursor() as cur:
        execute_values(
            cur,
            """
            INSERT INTO stg_egisz_outbound_documents (
                document_id, sent_at, reply_to, gost_jid_token, kind_code, jid, egmid, synced_at
            ) VALUES %s
            """,
            tuples,
            template=template,
        )


def fetch_pg_sync_snapshot(con, pipeline: str) -> dict[str, Any]:  # type: ignore[no-untyped-def]
    """Последняя активность витрины, курсор LOGID и MAX(EGMID) из staging исходящих."""
    with con.cursor() as cur:
        cur.execute(
            """
            SELECT
                (SELECT MAX(ts) FROM (
                    SELECT MAX(processed_at) AS ts FROM fact_egisz_transactions
                    UNION ALL
                    SELECT MAX(synced_at) AS ts FROM stg_egisz_outbound_documents
                    UNION ALL
                    SELECT MAX(updated_at) AS ts FROM etl_state WHERE pipeline = %s
                ) s) AS last_record_at,
                (SELECT last_log_id FROM etl_state WHERE pipeline = %s) AS last_log_id,
                (SELECT MAX(egmid) FROM stg_egisz_outbound_documents) AS max_egmid
            """,
            (pipeline, pipeline),
        )
        row = cur.fetchone()
    last_at, lid_raw, eg_raw = row if row else (None, None, None)
    out: dict[str, Any] = {
        "sync_at": last_at.isoformat() if last_at else None,
        "log_id": int(lid_raw) if lid_raw is not None else None,
        "egmid": int(eg_raw) if eg_raw is not None else None,
    }
    return out


def upsert_facts_batch(con, rows: list[dict[str, Any]]) -> None:  # type: ignore[no-untyped-def]
    """Batch UPSERT into fact_egisz_transactions."""
    if not rows:
        return
    # execute_values + ON CONFLICT: duplicate relates_to_id in one statement is rejected (CardinalityViolation).
    dedup: dict[str, dict[str, Any]] = {}
    for r in rows:
        dedup[r["relates_to_id"]] = r
    rows = list(dedup.values())
    tuples: list[tuple[Any, ...]] = []
    for r in rows:
        err = r["errors_json"]
        if isinstance(err, str):
            err = json.loads(err)
        tuples.append(
            (
                r["relates_to_id"],
                r.get("local_uid_semd"),
                r["jid"],
                r["gost_jid_token"],
                r["org_oid"],
                r["kind_code"],
                r["status"],
                r["emdr_id"],
                Json(err),
                r["registration_date"],
                r.get("semd_creation_at"),
                r["processed_at"],
            )
        )
    template = "(%s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb, %s, %s, %s)"  # Json() → jsonb
    with con.cursor() as cur:
        execute_values(
            cur,
            """
            INSERT INTO fact_egisz_transactions (
                relates_to_id, local_uid_semd, jid, gost_jid_token, org_oid, kind_code, status,
                emdr_id, errors_json, registration_date, semd_creation_at, processed_at
            ) VALUES %s
            ON CONFLICT (relates_to_id) DO UPDATE SET
                local_uid_semd = EXCLUDED.local_uid_semd,
                jid = EXCLUDED.jid,
                gost_jid_token = EXCLUDED.gost_jid_token,
                org_oid = EXCLUDED.org_oid,
                kind_code = EXCLUDED.kind_code,
                status = EXCLUDED.status,
                emdr_id = EXCLUDED.emdr_id,
                errors_json = EXCLUDED.errors_json,
                registration_date = EXCLUDED.registration_date,
                semd_creation_at = EXCLUDED.semd_creation_at,
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
