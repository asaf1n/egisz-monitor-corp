"""Firebird → PostgreSQL ETL for corp fact table (LOGID cursor, not MODIFYDATE)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable, TypedDict

from egisz_monitor_corp.config_loader import CorpAppConfig, load_corp_config
from egisz_monitor_corp.fb_client import fetch_all
from egisz_monitor_corp.parser import EgiszMonitorParser, StagingParseError, _norm_kind_code
from egisz_monitor_corp.pg_warehouse import (
    apply_sql_files,
    connect_pg,
    ensure_etl_state_table,
    get_last_log_id,
    insert_staging_errors,
    refresh_outbound_documents_staging,
    set_last_log_id,
    upsert_dim_clinic,
    upsert_dim_semd,
    upsert_facts_batch,
)
from egisz_monitor_corp.sql_util import (
    default_exchangelog_select,
    enrichment_egisz_licenses_sql,
    enrichment_jpersons_sql,
    exchangelog_count_after_cursor,
    exchangelog_count_window_after_cursor,
    outbound_documents_staging_select,
    paginated_exchangelog_sql,
)


@dataclass
class EtlRunStats:
    fetched: int
    facts_upserted: int
    staging_errors: int
    max_log_id: int
    last_cursor_after: int


class EtlProgressPayload(TypedDict, total=False):
    """Снимок прогресса для UI / логов (все поля опциональны кроме phase)."""

    phase: str
    total_rows: int
    loaded_rows: int
    parsed_facts: int
    staging_errors: int
    page: int
    outbound_loaded: int
    outbound_total: int
    journal_facts: int


def _to_int(v: Any) -> int | None:
    if v is None:
        return None
    try:
        n = int(v)
        return n if n > 0 else None
    except (TypeError, ValueError):
        return None


def _to_str(v: Any) -> str | None:
    if v is None:
        return None
    s = str(v).strip()
    return s or None


def _sent_at_utc(v: Any) -> datetime | None:
    if v is None:
        return None
    if isinstance(v, datetime):
        dt = v
        if dt.tzinfo is None:
            return dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    return None


def run_sync(
    cfg: CorpAppConfig | None = None,
    *,
    dry_run: bool = False,
    progress_cb: Callable[[str], None] | None = None,
    progress_detail_cb: Callable[[EtlProgressPayload], None] | None = None,
) -> EtlRunStats:
    """
    Load enrichment from Firebird, paginate EXCHANGELOG by LOGID, parse MSGTEXT (SOAP) + LOGTEXT (host), UPSERT PG.
    """
    cfg = cfg or load_corp_config()

    def log(msg: str) -> None:
        if progress_cb:
            progress_cb(msg)

    def detail(payload: EtlProgressPayload) -> None:
        if progress_detail_cb:
            progress_detail_cb(payload)

    base_sql = (cfg.etl.source_query or "").strip() or default_exchangelog_select(cfg.etl.sync_window_days)
    pipeline = cfg.etl.pipeline_name
    batch = max(1, cfg.etl.batch_size)

    mo_uid_to_jid_from_egisz_licenses: dict[str, int] = {}
    clinics: list[tuple[int, str | None, str | None, str | None, str | None]] = []
    jname_by_jid: dict[int, str] = {}

    log("Fetching EGISZ_LICENSES + JPERSONS from Firebird...")
    egisz_licenses_rows = fetch_all(cfg.firebird, enrichment_egisz_licenses_sql())
    jpersons_by_jid: dict[int, tuple[str | None, str | None, str | None]] = {}
    for r in fetch_all(cfg.firebird, enrichment_jpersons_sql()):
        jpj = _to_int(r.get("jid"))
        if jpj:
            jn = _to_str(r.get("jname"))
            jpersons_by_jid[jpj] = (
                jn,
                _to_str(r.get("jinn")),
                _to_str(r.get("fir_oid")),
            )
            if jn:
                jname_by_jid[jpj] = jn
    for r in egisz_licenses_rows:
        mo = _to_str(r.get("mo_uid"))
        jid = _to_int(r.get("jid"))
        jn = _to_str(r.get("jname"))
        if mo and jid:
            mo_uid_to_jid_from_egisz_licenses[mo] = jid
        if jid:
            clinics.append(
                (
                    jid,
                    jn,
                    mo,
                    _to_str(r.get("jinn")),
                    _to_str(r.get("fir_oid")),
                )
            )
            if jn and jid not in jname_by_jid:
                jname_by_jid[jid] = jn

    pg = None if dry_run else connect_pg(cfg.postgres)
    staging_buffer: list[tuple[str | None, str, str, str | None]] = []
    fact_buffer: list[dict[str, Any]] = []
    fetched = 0
    facts = 0
    staging_n = 0
    max_log_id = 0

    try:
        if pg is not None:
            apply_sql_files(pg, "001_schema.sql", "002_etl_state.sql", "005_healthcheck.sql")
            ensure_etl_state_table(pg)

        last_id = 0
        if cfg.etl.full_scan:
            last_id = 0
        elif dry_run:
            # Read cursor from Postgres even in dry-run (no writes) to limit Firebird scan.
            pg_r = connect_pg(cfg.postgres)
            try:
                ensure_etl_state_table(pg_r)
                last_id = get_last_log_id(pg_r, pipeline)
            finally:
                pg_r.close()
        elif pg is not None:
            last_id = get_last_log_id(pg, pipeline)
        log(f"ETL cursor last_log_id={last_id} pipeline={pipeline} full_scan={cfg.etl.full_scan}")

        detail({"phase": "counting"})
        log("Подсчёт строк EXCHANGELOG для прогресса...")
        total_exchangelog = 0
        try:
            if (cfg.etl.source_query or "").strip():
                cnt_sql = exchangelog_count_after_cursor(base_sql, last_log_id=last_id)
            else:
                cnt_sql = exchangelog_count_window_after_cursor(
                    cfg.etl.sync_window_days, last_log_id=last_id
                )
            cnt_rows = fetch_all(cfg.firebird, cnt_sql)
            if cnt_rows:
                raw = cnt_rows[0].get("cnt")
                if raw is not None:
                    total_exchangelog = int(raw)
        except Exception as ex:  # pragma: no cover — сеть/FB
            log(f"Предупреждение: не удалось получить COUNT для прогресса ({ex}).")
        log(f"EXCHANGELOG к обработке (LOGID > {last_id}): {total_exchangelog} строк.")
        detail(
            {
                "phase": "exchangelog_ready",
                "total_rows": total_exchangelog,
                "loaded_rows": 0,
                "parsed_facts": 0,
                "journal_facts": 0,
                "staging_errors": staging_n,
            }
        )

        parser = EgiszMonitorParser()

        def on_stage(err: StagingParseError) -> None:
            nonlocal staging_n
            staging_buffer.append((err.relates_to_id, err.error_code, err.message, err.log_excerpt))
            staging_n += 1
            if pg is not None and len(staging_buffer) >= 200:
                insert_staging_errors(pg, staging_buffer)
                pg.commit()
                staging_buffer.clear()

        page = 0
        while True:
            page += 1
            sql = paginated_exchangelog_sql(base_sql, last_log_id=last_id, limit=batch)
            detail(
                {
                    "phase": "fetch_page",
                    "total_rows": total_exchangelog,
                    "loaded_rows": fetched,
                    "parsed_facts": facts,
                    "journal_facts": facts,
                    "staging_errors": staging_n,
                    "page": page,
                }
            )
            rows = fetch_all(cfg.firebird, sql)
            if not rows:
                break
            fetched += len(rows)

            for row_i, r in enumerate(rows, start=1):
                lid = _to_int(r.get("logid"))
                if lid and lid > max_log_id:
                    max_log_id = lid

                logtext = r.get("logtext")
                if logtext is not None and not isinstance(logtext, str):
                    logtext = str(logtext)
                msgtext = r.get("msgtext")
                if msgtext is not None and not isinstance(msgtext, str):
                    msgtext = str(msgtext)

                egisz_licenses_kind = r.get("egisz_licenses_kind")
                mo_uid = r.get("mo_uid")
                egisz_licenses_jid = _to_int(r.get("egisz_licenses_jid"))
                reply_to = _to_str(r.get("replyto"))
                doc_id = _to_str(r.get("documentid"))

                log_created = _sent_at_utc(r.get("log_created_at"))
                msg_created = _sent_at_utc(r.get("msg_created_at"))
                rec = parser.build_record(
                    logtext,
                    msg_text=msgtext,
                    kind_from_egisz_licenses=egisz_licenses_kind,
                    mo_uid_from_egisz_licenses=_to_str(mo_uid),
                    jid_from_egisz_licenses_row=egisz_licenses_jid,
                    jid_by_mo_uid_from_egisz_licenses=mo_uid_to_jid_from_egisz_licenses,
                    reply_to=reply_to,
                    document_id=doc_id,
                    msg_created_at=msg_created,
                    log_created_at=log_created,
                    on_staging_error=on_stage,
                )
                if progress_detail_cb and (row_i % 200 == 0 or row_i == len(rows)):
                    detail(
                        {
                            "phase": "parsing",
                            "total_rows": total_exchangelog,
                            "loaded_rows": fetched,
                            "parsed_facts": facts,
                            "journal_facts": facts,
                            "staging_errors": staging_n,
                            "page": page,
                        }
                    )

                if rec is None:
                    continue

                # Исключаем тестовые данные, чтобы они не искажали статистику
                jn = jname_by_jid.get(rec.jid) if rec.jid else None
                if jn and ("test" in jn.lower() or "тест" in jn.lower()):
                    continue

                fact_buffer.append(rec.as_fact_row())
                facts += 1

                if rec.kind_code and rec.kind_name:
                    if pg is not None:
                        upsert_dim_semd(pg, rec.kind_code, rec.kind_name)

                if rec.jid and rec.jid > 0:
                    jn_dim = jinn_v = fir_v = None
                    for jid, jname, mouid, jinn, fir_oid in clinics:
                        if jid == rec.jid:
                            jn_dim, jinn_v, fir_v = jname, jinn, fir_oid
                            break
                    if rec.jid in jpersons_by_jid:
                        pjn, pjinn, pfir = jpersons_by_jid[rec.jid]
                        jn_dim = jn_dim or pjn
                        jinn_v = jinn_v or pjinn
                        fir_v = fir_v or pfir
                    if pg is not None:
                        upsert_dim_clinic(
                            pg,
                            rec.jid,
                            jn_dim,
                            _to_str(mo_uid) or rec.org_oid,
                            jinn=jinn_v,
                            fir_oid=fir_v,
                        )

            if pg is not None and fact_buffer:
                upsert_facts_batch(pg, fact_buffer)
                pg.commit()
                fact_buffer.clear()

            if pg is not None and staging_buffer:
                insert_staging_errors(pg, staging_buffer)
                pg.commit()
                staging_buffer.clear()

            if max_log_id > last_id:
                last_id = max_log_id
                if pg is not None:
                    set_last_log_id(pg, pipeline, last_id)
                    pg.commit()

            log(f"page {page} rows={len(rows)} max_log_id={max_log_id}")
            detail(
                {
                    "phase": "page_done",
                    "total_rows": total_exchangelog,
                    "loaded_rows": fetched,
                    "parsed_facts": facts,
                    "journal_facts": facts,
                    "staging_errors": staging_n,
                    "page": page,
                }
            )

            if len(rows) < batch:
                break

        detail(
            {
                "phase": "exchangelog_done",
                "total_rows": total_exchangelog,
                "loaded_rows": fetched,
                "parsed_facts": facts,
                "journal_facts": facts,
                "staging_errors": staging_n,
            }
        )

        if pg is not None and fact_buffer:
            upsert_facts_batch(pg, fact_buffer)
            pg.commit()
        if pg is not None and staging_buffer:
            insert_staging_errors(pg, staging_buffer)
            pg.commit()

        if pg is not None and not dry_run:
            log("Refreshing stg_egisz_outbound_documents (v_rpt_documents_no_response)...")
            detail(
                {
                    "phase": "outbound_firebird",
                    "total_rows": total_exchangelog,
                    "loaded_rows": fetched,
                    "parsed_facts": facts,
                    "journal_facts": facts,
                    "staging_errors": staging_n,
                }
            )
            omsg = fetch_all(cfg.firebird, outbound_documents_staging_select(cfg.etl.sync_window_days))
            omsg_sorted = sorted(
                omsg,
                key=lambda row: _sent_at_utc(row.get("msg_sent_at")) or datetime.min.replace(tzinfo=timezone.utc),
                reverse=True,
            )
            outbound_n = len(omsg_sorted)
            detail(
                {
                    "phase": "outbound_fetch",
                    "outbound_total": outbound_n,
                    "outbound_loaded": 0,
                    "journal_facts": facts,
                    "total_rows": total_exchangelog,
                    "loaded_rows": fetched,
                    "staging_errors": staging_n,
                }
            )
            parser_oob = EgiszMonitorParser()
            stg_out: list[dict[str, Any]] = []
            seen_doc: set[str] = set()
            for oi, r in enumerate(omsg_sorted, start=1):
                did = _to_str(r.get("documentid"))
                skip = not did or did in seen_doc
                if not skip:
                    jid = _to_int(r.get("egisz_licenses_jid"))
                    jn = jname_by_jid.get(jid) if jid else None
                    if jn and ("test" in jn.lower() or "тест" in jn.lower()):
                        skip = True
                if not skip:
                    seen_doc.add(did)
                    reply_to = _to_str(r.get("replyto"))
                    host_part = parser_oob.extract_jid(None, reply_to=reply_to)
                    kraw = r.get("egisz_licenses_kind")
                    kc = _norm_kind_code(str(kraw).strip() if kraw is not None else None)
                    stg_out.append(
                        {
                            "document_id": did,
                            "sent_at": _sent_at_utc(r.get("msg_sent_at")),
                            "reply_to": reply_to,
                            "gost_jid_token": host_part.get("gost_jid_token"),
                            "kind_code": kc,
                            "jid": _to_int(r.get("egisz_licenses_jid")),
                            "egmid": _to_int(r.get("egmid")),
                        }
                    )
                if progress_detail_cb and (oi % 200 == 0 or oi == outbound_n):
                    detail(
                        {
                            "phase": "outbound_parse",
                            "outbound_total": outbound_n,
                            "outbound_loaded": oi,
                            "parsed_facts": len(stg_out),
                            "journal_facts": facts,
                            "total_rows": total_exchangelog,
                            "loaded_rows": fetched,
                            "staging_errors": staging_n,
                        }
                    )
            og_total = len(stg_out)
            detail(
                {
                    "phase": "outbound_postgres",
                    "outbound_total": og_total,
                    "outbound_loaded": 0,
                    "parsed_facts": og_total,
                    "journal_facts": facts,
                    "total_rows": total_exchangelog,
                    "loaded_rows": fetched,
                    "staging_errors": staging_n,
                }
            )
            refresh_outbound_documents_staging(pg, stg_out)
            pg.commit()
            detail(
                {
                    "phase": "outbound_done",
                    "outbound_total": og_total,
                    "outbound_loaded": og_total,
                    "parsed_facts": og_total,
                    "journal_facts": facts,
                    "total_rows": total_exchangelog,
                    "loaded_rows": fetched,
                    "staging_errors": staging_n,
                }
            )

        cursor_after = get_last_log_id(pg, pipeline) if pg is not None else last_id
        return EtlRunStats(
            fetched=fetched,
            facts_upserted=facts,
            staging_errors=staging_n,
            max_log_id=max_log_id,
            last_cursor_after=cursor_after,
        )
    finally:
        if pg is not None:
            pg.close()
