"""PostgreSQL warehouse: schema apply, UPSERT fact, dimensions, ETL state, staging errors."""

from __future__ import annotations

import hashlib
import json
import os
import random
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from egisz_monitor_corp.config_loader import PostgresConfig
from egisz_monitor_corp.parser import canonical_semd_document_uid

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


def terminate_other_sessions_with_advisory_locks(con) -> list[dict[str, Any]]:
    """Завершить другие сессии, удержавшие granted advisory lock на текущей БД (зависший ETL / UI).

    ``pg_terminate_backend`` на чужие PID обычно требует суперпользователя PostgreSQL либо роли
    с правом сигналить бэкенды; иначе в ответе будет ``terminated: false`` с текстом ошибки.
    """
    out: list[dict[str, Any]] = []
    with con.cursor() as cur:
        cur.execute(
            """
            SELECT DISTINCT a.pid, a.usename, a.application_name, a.state
            FROM pg_locks l
            JOIN pg_stat_activity a ON a.pid = l.pid
            WHERE l.locktype = 'advisory'
              AND l.granted
              AND a.datname = current_database()
              AND a.pid <> pg_backend_pid()
            """
        )
        meta = {row[0]: {"usename": row[1], "application_name": row[2], "state": row[3]} for row in cur.fetchall()}
    for pid, m in meta.items():
        try:
            with con.cursor() as cur2:
                cur2.execute("SELECT pg_terminate_backend(%s)", (pid,))
                row = cur2.fetchone()
                ok = bool(row and row[0])
            entry: dict[str, Any] = {"pid": pid, "terminated": ok, **m}
            out.append(entry)
        except Exception as e:  # pragma: no cover - privilege
            out.append({"pid": pid, "terminated": False, "error": str(e), **m})
    return out


def sql_dir() -> Path:
    """Репозиторий: <root>/sql. Wheel в контейнере: задайте EGISZ_MONITOR_SQL_DIR (см. docker/web/Dockerfile)."""
    override = (os.environ.get("EGISZ_MONITOR_SQL_DIR") or "").strip()
    if override:
        return Path(override)
    return Path(__file__).resolve().parent.parent / "sql"


def reports_schema_sql_filenames() -> tuple[str, ...]:
    """Имена .sql для витрины egisz_reports (порядок = sql/schema_apply_order.txt)."""
    order = sql_dir() / "schema_apply_order.txt"
    if not order.is_file():
        raise FileNotFoundError(f"Schema apply manifest missing: {order}")
    names: list[str] = []
    for raw in order.read_text(encoding="utf-8").splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        names.append(line)
    if not names:
        raise ValueError(f"No SQL filenames in {order}")
    return tuple(names)


def apply_reports_schema(con) -> None:  # type: ignore[no-untyped-def]
    """Идемпотентный DDL витрины: тот же набор, что k8s Job egisz-reports-schema-init."""
    apply_sql_files(con, *reports_schema_sql_filenames())


def _pg_is_transient_lock_error(e: Exception) -> bool:
    """Ошибки, после которых можно безопасно повторить запрос (deadlock/lock-timeout)."""
    if not isinstance(e, psycopg2.Error):
        return False
    code = getattr(e, "pgcode", None)
    # 40P01: deadlock_detected; 55P03: lock_not_available; 57014: query_canceled (lock/statement timeout)
    return code in {"40P01", "55P03", "57014"}


def _with_pg_retries(
    con,
    *,
    what: str,
    fn,  # type: ignore[no-untyped-def]
    attempts: int = 4,
    base_sleep_sec: float = 0.35,
) -> None:
    """Повторить транзакционный блок на transient lock/deadlock ошибках."""
    last_err: Exception | None = None
    for i in range(1, max(1, int(attempts)) + 1):
        try:
            fn()
            return
        except Exception as e:  # pragma: no cover - зависит от конкуренции в PG
            last_err = e
            try:
                con.rollback()
            except Exception:
                pass
            if not _pg_is_transient_lock_error(e) or i >= attempts:
                raise
            sleep_for = (base_sleep_sec * (2 ** (i - 1))) + (random.random() * 0.15)
            time.sleep(min(3.0, sleep_for))
    if last_err is not None:  # pragma: no cover - defensive
        raise last_err


def apply_sql_files(con, *names: str) -> None:  # type: ignore[no-untyped-def]
    """Execute bundled .sql files in order (idempotent DDL)."""
    root = sql_dir()
    for name in names:
        path = root / name
        if not path.is_file():
            raise FileNotFoundError(f"SQL file missing: {path}")
        sql_text = path.read_text(encoding="utf-8")

        def _one_file() -> None:
            with con.cursor() as cur:
                # DDL может конфликтовать с параллельными SELECT (Metabase/Config UI).
                # 5 секунд часто недостаточно при активных дашбордах (Metabase держит соединения/транзакции).
                # DDL применяется редко и идемпотентно, поэтому допускаем больше ожидания.
                cur.execute("SET LOCAL lock_timeout = '300s'")
                cur.execute(sql_text)
            con.commit()

        _with_pg_retries(con, what=f"apply_sql_files:{name}", fn=_one_file)


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


def prune_stg_egisz_messages_journal_by_sync_window(con, sync_window_days: int | None) -> None:  # type: ignore[no-untyped-def]
    """Удалить из staging сообщения старше окна CREATEDATE (как sync_window_days у журнала)."""
    d = int(sync_window_days) if sync_window_days is not None else 0
    if d <= 0:
        return
    with con.cursor() as cur:
        cur.execute(
            """
            DELETE FROM stg_egisz_messages_journal
            WHERE msg_created_at IS NOT NULL
              AND msg_created_at < (NOW() - (%s * INTERVAL '1 day'))
            """,
            (d,),
        )


def fetch_etl_watermark_row(con, pipeline: str) -> dict[str, int] | None:  # type: ignore[no-untyped-def]
    """Сырые водяные знаки etl_state для диагностики (без MAX по витрине). Нет строки — None."""
    with con.cursor() as cur:
        cur.execute(
            """
            SELECT
              last_log_id,
              COALESCE(last_egmid, 0)
            FROM etl_state
            WHERE pipeline = %s
            LIMIT 1
            """,
            (pipeline,),
        )
        row = cur.fetchone()
    if not row:
        return None
    lid_raw, eg_raw = row
    return {
        "last_log_id": int(lid_raw) if lid_raw is not None else 0,
        "last_egmid": int(eg_raw) if eg_raw is not None else 0,
    }


def fetch_etl_source_peaks_from_pg(con, pipeline: str) -> dict[str, Any]:  # type: ignore[no-untyped-def]
    """Последние source_max_* из etl_state (после успешного ETL), без опроса Firebird."""
    with con.cursor() as cur:
        cur.execute(
            """
            SELECT source_max_licenses_modifydate
            FROM etl_state WHERE pipeline = %s
            """,
            (pipeline,),
        )
        row = cur.fetchone()
    if not row:
        return {"source_max_licenses_modifydate": None}
    (lic_raw,) = row
    lic_out: Any = None
    if lic_raw is not None:
        iso = getattr(lic_raw, "isoformat", None)
        lic_out = iso() if callable(iso) else lic_raw
    return {"source_max_licenses_modifydate": lic_out}


def set_etl_source_peaks(
    con,
    pipeline: str,
    max_licenses_modifydate: Any,
) -> None:  # type: ignore[no-untyped-def]
    """Записать в etl_state пик MODIFYDATE лицензий (кэш для UI без опроса Firebird)."""
    sets: list[str] = []
    params: list[Any] = []
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


def _dedupe_jpersons_by_jid(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Один ряд на JID (последняя строка побеждает), только с непустым JID."""
    by_jid: dict[int, dict[str, Any]] = {}
    for r in rows:
        jid_raw = r.get("jid")
        if jid_raw is None:
            continue
        try:
            jid = int(jid_raw)
        except (TypeError, ValueError):
            continue
        if jid <= 0:
            continue
        by_jid[jid] = r
    return list(by_jid.values())


def refresh_license_staging_from_firebird_exports(
    con,
    *,
    jpersons_rows: list[dict[str, Any]],
    license_rows: list[dict[str, Any]],
    insert_chunk_size: int = 2000,
) -> None:  # type: ignore[no-untyped-def]
    """TRUNCATE staging JPERSONS + лицензий; вставка из Firebird; сшивка JNAME/JINN/FIR_OID в PostgreSQL (UPDATE … FROM)."""
    jp = _dedupe_jpersons_by_jid(jpersons_rows)
    with con.cursor() as cur:
        cur.execute("TRUNCATE stg_jpersons_import, stg_egisz_licenses_import")
    if not jp and not license_rows:
        return
    jp_tuples: list[tuple[Any, ...]] = []
    for r in jp:
        jp_tuples.append(
            (
                int(r["jid"]),
                (str(r.get("jname")).strip() if r.get("jname") is not None else None) or None,
                (str(r.get("jinn")).strip() if r.get("jinn") is not None else None) or None,
                (str(r.get("fir_oid")).strip() if r.get("fir_oid") is not None else None) or None,
            )
        )
    if jp_tuples:
        with con.cursor() as cur:
            execute_values(
                cur,
                """
                INSERT INTO stg_jpersons_import (jid, jname, jinn, fir_oid) VALUES %s
                """,
                jp_tuples,
            )
    if not license_rows:
        return
    ins_chunk = max(200, min(int(insert_chunk_size), 20_000))
    for i in range(0, len(license_rows), ins_chunk):
        part = license_rows[i : i + ins_chunk]
        tuples: list[tuple[Any, ...]] = []
        for r in part:
            tuples.append(
                (
                    r.get("id"),
                    r.get("jid"),
                    (str(r.get("mo_uid")).strip() if r.get("mo_uid") is not None else None) or None,
                    (str(r.get("mo_domen")).strip() if r.get("mo_domen") is not None else None) or None,
                    r.get("modifydate"),
                    r.get("egisz_licenses_kind"),
                    None,
                    None,
                    None,
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
    with con.cursor() as cur:
        cur.execute(
            """
            UPDATE stg_egisz_licenses_import s
            SET
                jname = NULLIF(BTRIM(j.jname::text), ''),
                jinn = COALESCE(NULLIF(BTRIM(j.jinn::text), ''), ''),
                fir_oid = COALESCE(NULLIF(BTRIM(j.fir_oid::text), ''), '')
            FROM stg_jpersons_import j
            WHERE s.jid IS NOT NULL AND j.jid = s.jid
            """
        )


def _egmid_rank_sql(v: Any) -> int:
    """Целое сравнение EGMID для дедупликации снимка (-1 если не число)."""
    if v is None:
        return -1
    try:
        return int(v)
    except (TypeError, ValueError):
        return -1


def _dedupe_journal_snapshot_rows_by_msgid(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Один MSGID в одном INSERT — одна строка (PostgreSQL: ON CONFLICT не может обновить дважды за команду).

    В источнике возможны дубликаты MSGID при полном окне (sync_window_days = 0); для конфликта оставляем строку
    с **максимальным EGMID** (актуальная запись сообщения).
    """
    order: list[str] = []
    best: dict[str, dict[str, Any]] = {}
    best_eg: dict[str, int] = {}
    for r in rows:
        raw = r.get("msgid")
        if raw is None:
            continue
        mid = str(raw).strip()[:512]
        if not mid:
            continue
        eg = _egmid_rank_sql(r.get("egmid"))
        if mid not in best:
            order.append(mid)
            best[mid] = r
            best_eg[mid] = eg
            continue
        if eg > best_eg[mid]:
            best[mid] = r
            best_eg[mid] = eg
    return [best[m] for m in order]


def truncate_journal_messages_staging(con) -> None:  # type: ignore[no-untyped-def]
    with con.cursor() as cur:
        # TRUNCATE требует ACCESS EXCLUSIVE и легко конфликтует с Metabase SELECT по v_rpt_* (архив/очередь).
        # В full-rescan важнее гарантировать прогресс ETL, чем мгновенно очистить таблицу.
        cur.execute("DELETE FROM stg_egisz_messages_journal")


def insert_journal_messages_staging_rows(con, rows: list[dict[str, Any]]) -> None:  # type: ignore[no-untyped-def]
    """Вставка строк снимка EGISZ_MESSAGES: ключ msgid (= MSGID), egmid (= суррогатный ключ записи)."""
    if not rows:
        return
    rows = _dedupe_journal_snapshot_rows_by_msgid(rows)
    if not rows:
        return
    tuples: list[tuple[Any, ...]] = []
    for r in rows:
        msgid_raw = r.get("msgid")
        if msgid_raw is None:
            continue
        mid = str(msgid_raw).strip()
        if not mid:
            continue
        doc_raw = r.get("documentid")
        doc_cell = canonical_semd_document_uid(
            str(doc_raw).strip() if doc_raw is not None else None
        )
        tuples.append(
            (
                mid[:512],
                r.get("egmid"),
                r.get("replyto"),
                doc_cell,
                r.get("msg_created_at"),
            )
        )
    if not tuples:
        return
    with con.cursor() as cur:
        execute_values(
            cur,
            """
            INSERT INTO stg_egisz_messages_journal (msgid, egmid, replyto, documentid, msg_created_at)
            VALUES %s
            ON CONFLICT (msgid) DO UPDATE SET
                egmid = EXCLUDED.egmid,
                replyto = EXCLUDED.replyto,
                documentid = EXCLUDED.documentid,
                msg_created_at = EXCLUDED.msg_created_at,
                loaded_at = NOW()
            """,
            tuples,
        )


def journal_msgids_present_in_staging(con, msgids: list[str]) -> set[str]:  # type: ignore[no-untyped-def]
    """Множество MSGID из ``msgids``, которые уже есть в ``stg_egisz_messages_journal``."""
    if not msgids:
        return set()
    uniq: list[str] = []
    seen: set[str] = set()
    for x in msgids:
        s = str(x).strip() if x is not None else ""
        if s and s not in seen:
            seen.add(s)
            uniq.append(s[:512])
    if not uniq:
        return set()
    with con.cursor() as cur:
        cur.execute(
            "SELECT msgid FROM stg_egisz_messages_journal WHERE msgid = ANY(%s)",
            (uniq,),
        )
        return {str(row[0]).strip() for row in cur.fetchall() if row and row[0] is not None}


def fetch_journal_messages_by_msgids(con, msgids: list[str]) -> list[dict[str, Any]]:  # type: ignore[no-untyped-def]
    """Строки из stg_egisz_messages_journal по списку MSGID (EXCHANGELOG.MSGID = EGISZ_MESSAGES.MSGID)."""
    if not msgids:
        return []
    uniq: list[str] = []
    seen: set[str] = set()
    for x in msgids:
        s = str(x).strip() if x is not None else ""
        if s and s not in seen:
            seen.add(s)
            uniq.append(s)
    if not uniq:
        return []
    with con.cursor() as cur:
        cur.execute(
            """
            SELECT msgid, egmid, replyto, documentid, msg_created_at
            FROM stg_egisz_messages_journal
            WHERE msgid = ANY(%s)
            """,
            (uniq,),
        )
        cols = [d[0] for d in cur.description]
        out: list[dict[str, Any]] = []
        for row in cur.fetchall():
            out.append(dict(zip(cols, row)))
    return out


def fact_journal_msgids_present_in_facts(con, msgids: list[str]) -> set[str]:  # type: ignore[no-untyped-def]
    """Множество EXCHANGELOG.MSGID, которые уже представлены в fact_egisz_transactions.journal_msgid.

    Используется в режиме полного пересъёма (sync_window_days < 0), чтобы не раздувать `stg_channel_errors`
    ранними строками журнала без SOAP/relatesToMessage, если по этому MSGID факт уже собран из более поздней записи.
    """
    if not msgids:
        return set()
    uniq: list[str] = []
    seen: set[str] = set()
    for x in msgids:
        s = str(x).strip() if x is not None else ""
        if not s:
            continue
        s = s[:512]
        if s not in seen:
            seen.add(s)
            uniq.append(s)
    if not uniq:
        return set()
    with con.cursor() as cur:
        cur.execute(
            """
            SELECT journal_msgid
            FROM fact_egisz_transactions
            WHERE journal_msgid IS NOT NULL
              AND journal_msgid <> ''
              AND journal_msgid = ANY(%s)
            """,
            (uniq,),
        )
        return {str(row[0]).strip() for row in cur.fetchall() if row and row[0] is not None}


def refresh_licenses_import_staging(con, rows: list[dict[str, Any]]) -> None:  # type: ignore[no-untyped-def]
    """Обратная совместимость: снимок уже сшитый (JNAME на строках лицензий). Предпочтительно `refresh_license_staging_from_firebird_exports`."""
    with con.cursor() as cur:
        cur.execute("TRUNCATE stg_jpersons_import, stg_egisz_licenses_import")
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
    def _do_refresh() -> None:
        with con.cursor() as cur:
            # Удаление/перезапись staging допускает ожидание: оно совместимо с SELECT,
            # но может конфликтовать с редкими DDL/maintenance операциями.
            cur.execute("SET LOCAL lock_timeout = '300s'")
            cur.execute("DELETE FROM stg_egisz_outbound_documents")

        if rows:
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
            with con.cursor() as cur2:
                cur2.execute("SET LOCAL lock_timeout = '300s'")
                execute_values(
                    cur2,
                    """
                    INSERT INTO stg_egisz_outbound_documents (
                        document_id, sent_at, reply_to, gost_jid_token, kind_code, jid, egmid, synced_at
                    ) VALUES %s
                    """,
                    tuples,
                    template=template,
                )
        con.commit()

    _with_pg_retries(con, what="refresh_outbound_documents_staging", fn=_do_refresh)


def fetch_healthcheck_snapshot(con, *, top_clinics: int = 5) -> dict[str, Any]:  # type: ignore[no-untyped-def]
    """
    Снимок healthcheck витрины: сигналы, top-N проблемных клиник, прокси-БД.

    Использует представления из sql/005_healthcheck.sql:
      - v_health_signals — пять сигналов (error_rate_high, unknown_high,
        channel_errors_burst, queue_red_24h, cursor_stale).
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
                    etl_last_update, etl_last_log_id, etl_cursor_egmid
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
                    etl_cursor_egmid,
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
                    "etl_cursor_egmid": int(etl_cursor_egmid) if etl_cursor_egmid is not None else None,
                }
                # Исходящий staging может быть пуст (прогон без outbound / старая витрина), при этом
                # fact_egisz_transactions уже заполнена — те же сущности, что в Metabase.
                if int(stg_total or 0) == 0:
                    try:
                        cur.execute(
                            """
                            SELECT
                                COUNT(*)::bigint,
                                COUNT(*) FILTER (
                                    WHERE egisz_messages_egmid IS NULL OR egisz_messages_egmid = 0
                                )::bigint,
                                MAX(egisz_messages_egmid)
                            FROM fact_egisz_transactions
                            """
                        )
                        fact_row = cur.fetchone()
                        if fact_row:
                            fc, fnull, fmax = fact_row
                            pb = out["proxy_db"]
                            pb["fact_rows"] = int(fc or 0)
                            pb["fact_without_egmid"] = int(fnull or 0)
                            pb["fact_max_egmid"] = int(fmax) if fmax is not None else None
                    except psycopg2.Error as e2:  # pragma: no cover
                        con.rollback()
                        out["errors"].append(f"fact_egisz_transactions (fallback): {e2}")
        except psycopg2.Error as e:  # pragma: no cover
            con.rollback()
            out["errors"].append(f"v_health_proxy_db: {e}")

    return out


def fetch_pg_sync_snapshot(con, pipeline: str) -> dict[str, Any]:  # type: ignore[no-untyped-def]
    """Показатели из etl_state (курсоры) + fallbacks из витрины.

    Важно: при аварийном прерывании *после* сброса курсоров (режим full sync) UI может временно видеть
    last_log_id=0/last_egmid=0. Для диагностики показываем подсказки из витрины: max EGMID и max LOGID,
    даже если курсор в etl_state ещё нулевой.
    """
    with con.cursor() as cur:
        cur.execute(
            """
            SELECT
                last_log_id,
                COALESCE(last_egmid, 0),
                source_max_licenses_modifydate
            FROM etl_state
            WHERE pipeline = %s
            LIMIT 1
            """,
            (pipeline,),
        )
        row = cur.fetchone()
    if not row:
        facts_only = 0
        facts_log_only = 0
        try:
            with con.cursor() as cur2:
                cur2.execute("SET LOCAL statement_timeout = '5s'")
                cur2.execute(
                    """
                    SELECT COALESCE(MAX(egisz_messages_egmid), 0)
                    FROM fact_egisz_transactions
                    WHERE egisz_messages_egmid IS NOT NULL AND egisz_messages_egmid > 0
                    """
                )
                mx0 = cur2.fetchone()
                if mx0 and mx0[0] is not None:
                    facts_only = int(mx0[0])
                cur2.execute(
                    """
                    SELECT GREATEST(
                      COALESCE((SELECT MAX(exchangelog_log_id) FROM fact_egisz_transactions), 0),
                      COALESCE((SELECT MAX(exchangelog_log_id) FROM stg_channel_errors), 0)
                    )
                    """
                )
                mxl0 = cur2.fetchone()
                if mxl0 and mxl0[0] is not None:
                    facts_log_only = int(mxl0[0])
        except Exception:
            facts_only = 0
            facts_log_only = 0
        eg0 = facts_only if facts_only > 0 else None
        lid0 = facts_log_only if facts_log_only > 0 else None
        return {
            "log_id": lid0,
            "egmid": eg0,
            "etl_last_egmid": eg0,
            "licenses_modifydate": None,
        }
    lid_raw, last_egmid_raw, lic_raw = row
    log_id = int(lid_raw) if lid_raw is not None else None
    last_eg = int(last_egmid_raw) if last_egmid_raw is not None else 0
    eg_display = last_eg
    facts_max_eg = 0
    facts_max_log = 0
    try:
        with con.cursor() as cur:
            cur.execute("SET LOCAL statement_timeout = '5s'")
            cur.execute(
                """
                SELECT COALESCE(MAX(egisz_messages_egmid), 0)
                FROM fact_egisz_transactions
                WHERE egisz_messages_egmid IS NOT NULL AND egisz_messages_egmid > 0
                """
            )
            mx = cur.fetchone()
            if mx and mx[0] is not None:
                facts_max_eg = int(mx[0])
            cur.execute(
                """
                SELECT GREATEST(
                  COALESCE((SELECT MAX(exchangelog_log_id) FROM fact_egisz_transactions), 0),
                  COALESCE((SELECT MAX(exchangelog_log_id) FROM stg_channel_errors), 0)
                )
                """
            )
            mxl = cur.fetchone()
            if mxl and mxl[0] is not None:
                facts_max_log = int(mxl[0])
    except Exception:
        facts_max_eg = 0
        facts_max_log = 0
    if eg_display <= 0 and facts_max_eg > 0:
        eg_display = facts_max_eg
    if (log_id is None or log_id <= 0) and facts_max_log > 0:
        log_id = facts_max_log
    lic_iso = lic_raw.isoformat() if lic_raw is not None else None

    return {
        "log_id": log_id,
        "egmid": eg_display,
        "etl_last_egmid": eg_display,
        "licenses_modifydate": lic_iso,
    }


def upsert_facts_batch(
    con,
    rows: list[dict[str, Any]],
    *,
    chunk_size: int = 500,
    commit_each_chunk: bool = True,
    statement_timeout_sec: int | None = None,
) -> None:  # type: ignore[no-untyped-def]
    """Batch UPSERT в fact_egisz_transactions; при большом буфере — несколько execute_values + COMMIT между частями."""
    if not rows:
        return
    dedup: dict[str, dict[str, Any]] = {}
    for r in rows:
        dedup[r["relates_to_id"]] = r
    rows = list(dedup.values())
    cs = max(50, min(int(chunk_size), 10_000))
    template = "(%s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)"
    n = len(rows)
    multi = n > cs
    for start in range(0, n, cs):
        chunk = rows[start : start + cs]
        tuples: list[tuple[Any, ...]] = []
        for r in chunk:
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
                    r.get("exchangelog_log_id"),
                    r.get("egisz_messages_egmid"),
                    r.get("journal_msgid"),
                    r.get("jid_from_license"),
                    r.get("jid_from_gost_log"),
                    r.get("jid_from_gost_reply"),
                    r.get("gost_token_logtext"),
                    r.get("gost_token_replyto"),
                    r.get("jid_sources_mismatch", False),
                )
            )
        with con.cursor() as cur:
            if statement_timeout_sec is not None:
                cur.execute(
                    "SET LOCAL statement_timeout = %s",
                    (f"{max(5, int(statement_timeout_sec))}s",),
                )
            execute_values(
                cur,
                """
                INSERT INTO fact_egisz_transactions (
                    relates_to_id, local_uid_semd, jid, gost_jid_token, org_oid, kind_code, status,
                    emdr_id, errors_json, registration_date, semd_creation_at, processed_at,
                    exchangelog_log_id, egisz_messages_egmid, journal_msgid,
                    jid_from_license, jid_from_gost_log, jid_from_gost_reply,
                    gost_token_logtext, gost_token_replyto, jid_sources_mismatch
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
                    processed_at = EXCLUDED.processed_at,
                    exchangelog_log_id = EXCLUDED.exchangelog_log_id,
                    egisz_messages_egmid = EXCLUDED.egisz_messages_egmid,
                    journal_msgid = EXCLUDED.journal_msgid,
                    jid_from_license = EXCLUDED.jid_from_license,
                    jid_from_gost_log = EXCLUDED.jid_from_gost_log,
                    jid_from_gost_reply = EXCLUDED.jid_from_gost_reply,
                    gost_token_logtext = EXCLUDED.gost_token_logtext,
                    gost_token_replyto = EXCLUDED.gost_token_replyto,
                    jid_sources_mismatch = EXCLUDED.jid_sources_mismatch
                WHERE EXCLUDED.exchangelog_log_id IS NOT NULL
                  AND (
                    fact_egisz_transactions.exchangelog_log_id IS NULL
                    OR EXCLUDED.exchangelog_log_id >= fact_egisz_transactions.exchangelog_log_id
                  )
                """,
                tuples,
                template=template,
            )
        if commit_each_chunk and multi:
            con.commit()


def insert_staging_channel_errors(
    con,
    rows: list[
        tuple[
            str | None,
            str,
            str,
            str | None,
            int | None,
            int | None,
            str | None,
            str | None,
            str | None,
            str | None,
            str | None,
            str | None,
            str | None,
            datetime | None,
        ]
    ],
) -> None:  # type: ignore[no-untyped-def]
    if not rows:
        return
    with con.cursor() as cur:
        execute_batch(
            cur,
            """
            INSERT INTO stg_channel_errors (
                relates_to_id, error_code, message, log_excerpt,
                exchangelog_log_id, egisz_messages_egmid, journal_msgid,
                error_top_type, error_group, error_subtype,
                relates_to_hint, local_uid_hint, emdr_id_hint,
                proxy_context_at
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s);
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
