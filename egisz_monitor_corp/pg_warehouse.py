"""PostgreSQL warehouse: schema apply, UPSERT fact, dimensions, ETL state, staging errors."""

from __future__ import annotations

import hashlib
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


def _pipeline_lock_key(pipeline: str) -> int:
    """Стабильный bigint-ключ для pg_try_advisory_lock из имени пайплайна (первые 8 байт MD5)."""
    digest = hashlib.md5(pipeline.encode("utf-8")).digest()[:8]
    n = int.from_bytes(digest, "big", signed=False)
    # PostgreSQL advisory lock принимает bigint (signed int64).
    if n >= 1 << 63:
        n -= 1 << 64
    return n


def try_acquire_pipeline_lock(con, pipeline: str) -> bool:  # type: ignore[no-untyped-def]
    """Session-level advisory lock: защищает run_sync от параллельного запуска (CronJob ↔ UI-кнопка).

    Lock освобождается автоматически при разрыве соединения (Postgres) — это устраняет
    залипание после crash'а воркера. Используется именно session-level (не xact), чтобы
    держать лок поверх множества коммитов внутри run_sync.
    """
    key = _pipeline_lock_key(pipeline)
    with con.cursor() as cur:
        cur.execute("SELECT pg_try_advisory_lock(%s)", (key,))
        row = cur.fetchone()
    con.commit()
    return bool(row and row[0])


def release_pipeline_lock(con, pipeline: str) -> None:  # type: ignore[no-untyped-def]
    """Освобождает session-level advisory lock; идемпотентно при пропуске лока."""
    key = _pipeline_lock_key(pipeline)
    try:
        with con.cursor() as cur:
            cur.execute("SELECT pg_advisory_unlock(%s)", (key,))
            cur.fetchone()
        con.commit()
    except Exception:  # pragma: no cover - rollback при закрытом con
        try:
            con.rollback()
        except Exception:
            pass


class PipelineLockBusyError(RuntimeError):
    """Бросаем, когда другой процесс уже держит advisory lock пайплайна."""


def connect_pg(cfg: PostgresConfig):  # type: ignore[no-untyped-def]
    # statement_timeout (5 мин) защищает sync и UI от зависших SELECT/UPSERT на стороне PG;
    # idle_in_transaction_session_timeout (10 мин) убивает «забытые» транзакции при крэше воркера.
    # SET LOCAL внутри fetch_healthcheck_snapshot переопределяет эти значения локально (10s) и не конфликтует.
    pg_options = (
        f"-c search_path={cfg.schema} "
        "-c statement_timeout=300000 "
        "-c idle_in_transaction_session_timeout=600000"
    )
    con = psycopg2.connect(
        host=cfg.host,
        port=cfg.port,
        dbname=cfg.database,
        user=cfg.user,
        password=cfg.password,
        options=pg_options,
        connect_timeout=10,
    )
    con.set_client_encoding("UTF8")
    con.autocommit = False
    return con


def sql_dir() -> Path:
    """Репозиторий: <root>/sql. Wheel в контейнере: задайте EGISZ_MONITOR_SQL_DIR (см. docker/web/Dockerfile)."""
    override = (os.environ.get("EGISZ_MONITOR_SQL_DIR") or "").strip()
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


def get_last_egmid(con, pipeline: str) -> int:  # type: ignore[no-untyped-def]
    with con.cursor() as cur:
        cur.execute(
            "SELECT COALESCE(last_egmid, 0) FROM etl_state WHERE pipeline = %s",
            (pipeline,),
        )
        row = cur.fetchone()
        return int(row[0]) if row else 0


def set_last_egmid(con, pipeline: str, last_egmid: int) -> None:  # type: ignore[no-untyped-def]
    with con.cursor() as cur:
        cur.execute(
            """
            INSERT INTO etl_state (pipeline, last_log_id, last_egmid, updated_at)
            VALUES (%s, 0, %s, NOW())
            ON CONFLICT (pipeline) DO UPDATE
            SET last_egmid = EXCLUDED.last_egmid, updated_at = NOW();
            """,
            (pipeline, last_egmid),
        )


def fetch_etl_source_peaks_from_pg(con, pipeline: str) -> dict[str, Any]:  # type: ignore[no-untyped-def]
    """Последние source_max_* из etl_state (после успешного ETL), без опроса Firebird."""
    with con.cursor() as cur:
        cur.execute(
            """
            SELECT source_max_egmid, source_max_licenses_modifydate
            FROM etl_state WHERE pipeline = %s
            """,
            (pipeline,),
        )
        row = cur.fetchone()
    if not row:
        return {"source_max_egmid": None, "source_max_licenses_modifydate": None}
    eg_raw, lic_raw = row
    eg = int(eg_raw) if eg_raw is not None else None
    lic_out: Any = None
    if lic_raw is not None:
        iso = getattr(lic_raw, "isoformat", None)
        lic_out = iso() if callable(iso) else lic_raw
    return {"source_max_egmid": eg, "source_max_licenses_modifydate": lic_out}


def set_etl_source_peaks(
    con,
    pipeline: str,
    max_egmid: Any,
    max_licenses_modifydate: Any,
) -> None:  # type: ignore[no-untyped-def]
    """Записать в etl_state «пики» источника (MAX EGMID / MAX MODIFYDATE лицензий), см. ETL sync."""
    sets: list[str] = []
    params: list[Any] = []
    if max_egmid is not None:
        sets.append("source_max_egmid = %s")
        params.append(int(max_egmid))
    if max_licenses_modifydate is not None:
        sets.append("source_max_licenses_modifydate = %s")
        params.append(max_licenses_modifydate)
    if not sets:
        return
    sets.append("source_peaks_updated_at = NOW()")
    params.append(pipeline)
    sql = f"UPDATE etl_state SET {', '.join(sets)} WHERE pipeline = %s"
    with con.cursor() as cur:
        cur.execute(sql, tuple(params))


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


def refresh_licenses_import_staging(con, rows: list[dict[str, Any]]) -> None:  # type: ignore[no-untyped-def]
    """Полная перезапись сырого снимка лицензий; дальше merge в dim_clinics и выборка для ETL — в PostgreSQL."""
    with con.cursor() as cur:
        cur.execute("TRUNCATE stg_egisz_licenses_import")
    if not rows:
        return
    tuples: list[tuple[Any, ...]] = []
    for r in rows:
        tuples.append(
            (
                r.get("id"),
                r.get("jid"),
                (str(r.get("mo_uid")).strip() if r.get("mo_uid") is not None else None) or None,
                (str(r.get("mo_domen")).strip() if r.get("mo_domen") is not None else None) or None,
                r.get("modifydate"),
                r.get("egisz_licenses_kind"),
                r.get("jname"),
                r.get("jinn"),
                r.get("fir_oid"),
            )
        )
    with con.cursor() as cur:
        execute_values(
            cur,
            """
            INSERT INTO stg_egisz_licenses_import (
                fb_id, jid, mo_uid, mo_domen, modifydate, egisz_licenses_kind, jname, jinn, fir_oid
            ) VALUES %s
            """,
            tuples,
        )


def merge_dim_clinics_from_license_staging(con) -> None:  # type: ignore[no-untyped-def]
    """UPSERT dim_clinics из staging: только строки с JID; приоритет свежей MODIFYDATE (как в однострочном upsert_dim_clinic)."""
    with con.cursor() as cur:
        cur.execute(
            """
            INSERT INTO dim_clinics (jid, jname, mo_uid, jinn, fir_oid, updated_at)
            SELECT DISTINCT ON (s.jid)
                s.jid::bigint,
                NULLIF(BTRIM(s.jname::text), ''),
                COALESCE(NULLIF(BTRIM(s.mo_uid::text), ''), ''),
                COALESCE(NULLIF(BTRIM(s.jinn::text), ''), ''),
                COALESCE(NULLIF(BTRIM(s.fir_oid::text), ''), ''),
                NOW()
            FROM stg_egisz_licenses_import s
            WHERE s.jid IS NOT NULL
            ORDER BY s.jid, s.modifydate DESC NULLS LAST, s.fb_id DESC NULLS LAST
            ON CONFLICT (jid) DO UPDATE SET
                jname = COALESCE(EXCLUDED.jname, dim_clinics.jname),
                mo_uid = CASE WHEN EXCLUDED.mo_uid <> '' THEN EXCLUDED.mo_uid ELSE dim_clinics.mo_uid END,
                jinn = CASE WHEN EXCLUDED.jinn <> '' THEN EXCLUDED.jinn ELSE dim_clinics.jinn END,
                fir_oid = CASE WHEN EXCLUDED.fir_oid <> '' THEN EXCLUDED.fir_oid ELSE dim_clinics.fir_oid END,
                updated_at = NOW();
            """
        )


def fetch_license_rows_for_enrichment(con) -> list[dict[str, Any]]:  # type: ignore[no-untyped-def]
    """Строки для кэша ETL после очистки на стороне PostgreSQL (только с непустым JID)."""
    with con.cursor() as cur:
        cur.execute(
            """
            SELECT
                fb_id AS id,
                jid,
                mo_uid,
                mo_domen,
                modifydate,
                egisz_licenses_kind,
                jname,
                jinn,
                fir_oid
            FROM stg_egisz_licenses_import
            WHERE jid IS NOT NULL
            ORDER BY jid, modifydate DESC NULLS LAST, fb_id DESC NULLS LAST
            """
        )
        cols = [d[0] for d in cur.description]
        out: list[dict[str, Any]] = []
        for row in cur.fetchall():
            out.append(dict(zip(cols, row)))
    return out


def refresh_outbound_documents_staging(con, rows: list[dict[str, Any]]) -> None:  # type: ignore[no-untyped-def]
    """Полная перезапись stg_egisz_outbound_documents снимком (порядок строк как во входном iterable — типично EGMID DESC)."""
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


def fetch_healthcheck_snapshot(con, *, top_clinics: int = 5) -> dict[str, Any]:  # type: ignore[no-untyped-def]
    """
    Снимок healthcheck витрины: сигналы, top-N проблемных клиник, прокси-БД.

    Использует представления из sql/005_healthcheck.sql:
      - v_health_signals — пять сигналов (error_rate_high, unknown_high,
        parse_errors_burst, queue_red_24h, cursor_stale).
      - v_health_by_clinic — агрегаты по клиникам за 24h.
      - v_health_proxy_db — счётчики staging исходящих + последний апдейт ETL.

    Краткий statement_timeout (10 секунд) защищает Config UI от зависания при
    долгом сканировании.
    """
    out: dict[str, Any] = {
        "signals": [],
        "by_clinic_top": [],
        "proxy_db": {},
        "level_summary": {"red": 0, "yellow": 0, "green": 0},
        "errors": [],
    }
    with con.cursor() as cur:
        cur.execute("SET LOCAL statement_timeout = '10s'")
        try:
            cur.execute(
                """
                SELECT code, title, level, value, value_unit, denominator, hint
                FROM v_health_signals
                ORDER BY CASE level
                    WHEN 'red' THEN 0
                    WHEN 'yellow' THEN 1
                    WHEN 'green' THEN 2
                    ELSE 3
                END, code
                """
            )
            for code, title, level, value, value_unit, denominator, hint in cur.fetchall():
                lvl = (level or "green").lower()
                if lvl in out["level_summary"]:
                    out["level_summary"][lvl] += 1
                out["signals"].append(
                    {
                        "code": code,
                        "title": title,
                        "level": lvl,
                        "value": float(value) if value is not None else None,
                        "value_unit": value_unit,
                        "denominator": int(denominator) if denominator is not None else None,
                        "hint": hint,
                    }
                )
        except psycopg2.Error as e:  # pragma: no cover - relies on schema in PG
            con.rollback()
            out["errors"].append(f"v_health_signals: {e}")

    with con.cursor() as cur:
        cur.execute("SET LOCAL statement_timeout = '10s'")
        try:
            cur.execute(
                """
                SELECT
                    jid, clinic_name, clinic_inn, clinic_mo_oid,
                    facts_24h, success_24h, errors_24h, unknown_24h,
                    error_rate_24h, unknown_rate_24h, pending_now,
                    last_seen_at, health_level
                FROM v_health_by_clinic
                ORDER BY
                    CASE health_level
                        WHEN 'red' THEN 0
                        WHEN 'yellow' THEN 1
                        ELSE 2
                    END,
                    error_rate_24h DESC NULLS LAST,
                    pending_now DESC NULLS LAST,
                    facts_24h DESC NULLS LAST
                LIMIT %s
                """,
                (int(top_clinics),),
            )
            for row in cur.fetchall():
                (
                    jid,
                    clinic_name,
                    inn,
                    fir,
                    facts_24h,
                    success_24h,
                    errors_24h,
                    unknown_24h,
                    error_rate_24h,
                    unknown_rate_24h,
                    pending_now,
                    last_seen_at,
                    health_level,
                ) = row
                out["by_clinic_top"].append(
                    {
                        "jid": int(jid) if jid is not None else None,
                        "clinic_name": clinic_name,
                        "clinic_inn": inn,
                        "clinic_mo_oid": fir,
                        "facts_24h": int(facts_24h or 0),
                        "success_24h": int(success_24h or 0),
                        "errors_24h": int(errors_24h or 0),
                        "unknown_24h": int(unknown_24h or 0),
                        "error_rate_24h": float(error_rate_24h) if error_rate_24h is not None else None,
                        "unknown_rate_24h": float(unknown_rate_24h) if unknown_rate_24h is not None else None,
                        "pending_now": int(pending_now or 0),
                        "last_seen_at": last_seen_at.isoformat() if last_seen_at else None,
                        "health_level": (health_level or "green").lower(),
                    }
                )
        except psycopg2.Error as e:  # pragma: no cover
            con.rollback()
            out["errors"].append(f"v_health_by_clinic: {e}")

    with con.cursor() as cur:
        cur.execute("SET LOCAL statement_timeout = '10s'")
        try:
            cur.execute(
                """
                SELECT
                    stg_outbound_total, stg_without_egmid, stg_without_jid,
                    staging_max_egmid, staging_max_sent_at,
                    pending_total, pending_1h, pending_1_24h, pending_older_24h,
                    etl_last_update, etl_last_log_id
                FROM v_health_proxy_db
                """
            )
            row = cur.fetchone()
            if row:
                (
                    stg_total,
                    stg_no_egmid,
                    stg_no_jid,
                    staging_max_egmid,
                    staging_max_sent_at,
                    pending_total,
                    pending_1h,
                    pending_1_24h,
                    pending_older_24h,
                    etl_last_update,
                    etl_last_log_id,
                ) = row
                out["proxy_db"] = {
                    "stg_outbound_total": int(stg_total or 0),
                    "stg_without_egmid": int(stg_no_egmid or 0),
                    "stg_without_jid": int(stg_no_jid or 0),
                    "staging_max_egmid": int(staging_max_egmid) if staging_max_egmid is not None else None,
                    "staging_max_sent_at": staging_max_sent_at.isoformat() if staging_max_sent_at else None,
                    "pending_total": int(pending_total or 0),
                    "pending_1h": int(pending_1h or 0),
                    "pending_1_24h": int(pending_1_24h or 0),
                    "pending_older_24h": int(pending_older_24h or 0),
                    "etl_last_update": etl_last_update.isoformat() if etl_last_update else None,
                    "etl_last_log_id": int(etl_last_log_id) if etl_last_log_id is not None else None,
                }
        except psycopg2.Error as e:  # pragma: no cover
            con.rollback()
            out["errors"].append(f"v_health_proxy_db: {e}")

    return out


def fetch_pg_sync_snapshot(con, pipeline: str) -> dict[str, Any]:  # type: ignore[no-untyped-def]
    """Снимок для UI: last_log_id, last_egmid (последняя выгрузка EGISZ_MESSAGES), витрина, MAX(egmid) в staging, пики FB в etl_state."""
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
                    UNION ALL
                    SELECT source_peaks_updated_at AS ts FROM etl_state WHERE pipeline = %s AND source_peaks_updated_at IS NOT NULL
                ) s) AS last_record_at,
                (SELECT last_log_id FROM etl_state WHERE pipeline = %s) AS last_log_id,
                (SELECT COALESCE(last_egmid, 0) FROM etl_state WHERE pipeline = %s) AS last_egmid,
                (SELECT MAX(egmid) FROM stg_egisz_outbound_documents) AS max_egmid_staging,
                (SELECT source_max_egmid FROM etl_state WHERE pipeline = %s) AS source_max_egmid,
                (SELECT source_max_licenses_modifydate FROM etl_state WHERE pipeline = %s) AS source_max_licenses_modifydate,
                (SELECT source_peaks_updated_at FROM etl_state WHERE pipeline = %s) AS source_peaks_updated_at
            """,
            (pipeline, pipeline, pipeline, pipeline, pipeline, pipeline, pipeline),
        )
        row = cur.fetchone()
    (
        last_at,
        lid_raw,
        last_egmid_raw,
        eg_staging_raw,
        src_eg_raw,
        src_lic_raw,
        src_peaks_at,
    ) = row if row else (None, None, None, None, None, None, None)

    eg_staging = int(eg_staging_raw) if eg_staging_raw is not None else None
    src_eg = int(src_eg_raw) if src_eg_raw is not None else None
    last_eg = int(last_egmid_raw) if last_egmid_raw is not None else None
    lic_iso = src_lic_raw.isoformat() if src_lic_raw is not None else None

    # UI «EGMID»: max(last_egmid, source_max_egmid) — полный прогон поднимает оба; при сбое после выгрузки
    # сообщений source_max_egmid уже отражает последнюю выгрузку из FB, last_egmid — закоммиченный ватермарк.
    eg_candidates: list[int] = []
    if last_eg is not None:
        eg_candidates.append(last_eg)
    if src_eg is not None:
        eg_candidates.append(src_eg)
    eg_display = max(eg_candidates) if eg_candidates else eg_staging

    out: dict[str, Any] = {
        "sync_at": last_at.isoformat() if last_at else None,
        "log_id": int(lid_raw) if lid_raw is not None else None,
        "last_egmid": last_eg,
        "egmid": eg_display,
        "egmid_staging_max": eg_staging,
        "source_max_egmid": src_eg,
        "licenses_modifydate": lic_iso,
        "source_peaks_updated_at": src_peaks_at.isoformat() if src_peaks_at else None,
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
