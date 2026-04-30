"""Firebird → PostgreSQL ETL for corp fact table (LOGID cursor, not MODIFYDATE).

Архитектура `run_sync`:
  1. `_load_enrichment_cache` — Firebird → in-memory словари справочников (EGISZ_LICENSES + JPERSONS).
  2. `_count_exchangelog_total` — COUNT строк журнала за окном для прогресс-бара UI.
  3. `_process_exchangelog_pages` — пагинация по LOGID, парсинг MSGTEXT, UPSERT факта/измерений.
  4. `_refresh_outbound_documents` — снимок очереди исходящих в `stg_egisz_outbound_documents`.

Расщепление сделано для тестируемости (`tests/test_etl_*`) и читаемости логов: каждая
сабфаза публикует своё имя в `EtlProgressPayload.phase`, UI отображает русскую подпись.

Безопасность параллельного запуска: `pg_try_advisory_lock(hash(pipeline))` — CronJob и
UI-кнопка теперь не могут стартовать sync одновременно, выполнится только первый.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, TypedDict

from egisz_monitor_corp.config_loader import CorpAppConfig, load_corp_config
from egisz_monitor_corp.fb_client import fetch_all
from egisz_monitor_corp.parser import EgiszMonitorParser, StagingParseError, _norm_kind_code
from egisz_monitor_corp.pg_warehouse import (
    PipelineLockBusyError,
    apply_sql_files,
    connect_pg,
    ensure_etl_state_table,
    get_last_log_id,
    insert_staging_errors,
    refresh_outbound_documents_staging,
    release_pipeline_lock,
    set_last_log_id,
    try_acquire_pipeline_lock,
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


@dataclass
class EnrichmentCache:
    """Справочники Firebird, загруженные в начале run_sync (O(1) lookups в горячем цикле)."""

    mo_uid_to_jid_from_egisz_licenses: dict[str, int] = field(default_factory=dict)
    clinics: list[tuple[int, str | None, str | None, str | None, str | None]] = field(
        default_factory=list
    )
    jname_by_jid: dict[int, str] = field(default_factory=dict)
    jpersons_by_jid: dict[int, tuple[str | None, str | None, str | None]] = field(
        default_factory=dict
    )


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


def _is_test_clinic(jname: str | None) -> bool:
    """Тестовые клиники: jname содержит 'test' или 'тест' (в нижнем регистре). Решение зафиксировано в .cursorrules."""
    if not jname:
        return False
    low = jname.lower()
    return "test" in low or "тест" in low


def _load_enrichment_cache(cfg: CorpAppConfig, log: Callable[[str], None]) -> EnrichmentCache:
    """Загружает EGISZ_LICENSES + JPERSONS из Firebird в один проход (типично десятки тыс. строк)."""
    log("Fetching EGISZ_LICENSES + JPERSONS from Firebird...")
    egisz_licenses_rows = fetch_all(cfg.firebird, enrichment_egisz_licenses_sql())

    cache = EnrichmentCache()
    for r in fetch_all(cfg.firebird, enrichment_jpersons_sql()):
        jpj = _to_int(r.get("jid"))
        if jpj:
            jn = _to_str(r.get("jname"))
            cache.jpersons_by_jid[jpj] = (
                jn,
                _to_str(r.get("jinn")),
                _to_str(r.get("fir_oid")),
            )
            if jn:
                cache.jname_by_jid[jpj] = jn

    for r in egisz_licenses_rows:
        mo = _to_str(r.get("mo_uid"))
        jid = _to_int(r.get("jid"))
        jn = _to_str(r.get("jname"))
        if mo and jid:
            cache.mo_uid_to_jid_from_egisz_licenses[mo] = jid
        if jid:
            cache.clinics.append(
                (
                    jid,
                    jn,
                    mo,
                    _to_str(r.get("jinn")),
                    _to_str(r.get("fir_oid")),
                )
            )
            if jn and jid not in cache.jname_by_jid:
                cache.jname_by_jid[jid] = jn
    return cache


def _count_exchangelog_total(
    cfg: CorpAppConfig,
    base_sql: str,
    *,
    has_custom_query: bool,
    last_id: int,
    log: Callable[[str], None],
) -> int:
    """COUNT для прогресс-бара. Падение запроса не должно ломать sync — отдаём 0 и логируем."""
    try:
        if has_custom_query:
            cnt_sql = exchangelog_count_after_cursor(base_sql, last_log_id=last_id)
        else:
            cnt_sql = exchangelog_count_window_after_cursor(
                cfg.etl.sync_window_days, last_log_id=last_id
            )
        cnt_rows = fetch_all(cfg.firebird, cnt_sql)
        if cnt_rows:
            raw = cnt_rows[0].get("cnt")
            if raw is not None:
                return int(raw)
    except Exception as ex:  # pragma: no cover - сеть/FB
        log(f"Предупреждение: не удалось получить COUNT для прогресса ({ex}).")
    return 0


@dataclass
class _PageStats:
    fetched: int = 0
    facts: int = 0
    staging_n: int = 0
    max_log_id: int = 0
    last_id: int = 0


def _process_exchangelog_pages(
    cfg: CorpAppConfig,
    pg: Any,
    *,
    base_sql: str,
    enrichment: EnrichmentCache,
    last_id: int,
    total_exchangelog: int,
    progress_detail_cb: Callable[[EtlProgressPayload], None] | None,
    log: Callable[[str], None],
    detail: Callable[[EtlProgressPayload], None],
) -> _PageStats:
    """Пагинация по LOGID + парсинг MSGTEXT + UPSERT в PG. Курсор коммитится постранично."""
    pipeline = cfg.etl.pipeline_name
    batch = max(1, cfg.etl.batch_size)
    parser = EgiszMonitorParser()
    staging_buffer: list[tuple[str | None, str, str, str | None]] = []
    fact_buffer: list[dict[str, Any]] = []
    stats = _PageStats(last_id=last_id)

    def on_stage(err: StagingParseError) -> None:
        staging_buffer.append((err.relates_to_id, err.error_code, err.message, err.log_excerpt))
        stats.staging_n += 1
        if pg is not None and len(staging_buffer) >= 200:
            insert_staging_errors(pg, staging_buffer)
            pg.commit()
            staging_buffer.clear()

    page = 0
    while True:
        page += 1
        sql = paginated_exchangelog_sql(base_sql, last_log_id=stats.last_id, limit=batch)
        detail(
            {
                "phase": "fetch_page",
                "total_rows": total_exchangelog,
                "loaded_rows": stats.fetched,
                "parsed_facts": stats.facts,
                "journal_facts": stats.facts,
                "staging_errors": stats.staging_n,
                "page": page,
            }
        )
        rows = fetch_all(cfg.firebird, sql)
        if not rows:
            break
        stats.fetched += len(rows)

        for row_i, r in enumerate(rows, start=1):
            lid = _to_int(r.get("logid"))
            if lid and lid > stats.max_log_id:
                stats.max_log_id = lid

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
                jid_by_mo_uid_from_egisz_licenses=enrichment.mo_uid_to_jid_from_egisz_licenses,
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
                        "loaded_rows": stats.fetched,
                        "parsed_facts": stats.facts,
                        "journal_facts": stats.facts,
                        "staging_errors": stats.staging_n,
                        "page": page,
                    }
                )

            if rec is None:
                continue

            jn = enrichment.jname_by_jid.get(rec.jid) if rec.jid else None
            if _is_test_clinic(jn):
                continue

            fact_buffer.append(rec.as_fact_row())
            stats.facts += 1

            if rec.kind_code and rec.kind_name and pg is not None:
                upsert_dim_semd(pg, rec.kind_code, rec.kind_name)

            if rec.jid and rec.jid > 0:
                jn_dim = jinn_v = fir_v = None
                for jid, jname, mouid, jinn, fir_oid in enrichment.clinics:
                    if jid == rec.jid:
                        jn_dim, jinn_v, fir_v = jname, jinn, fir_oid
                        break
                if rec.jid in enrichment.jpersons_by_jid:
                    pjn, pjinn, pfir = enrichment.jpersons_by_jid[rec.jid]
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

        if stats.max_log_id > stats.last_id:
            stats.last_id = stats.max_log_id
            if pg is not None:
                set_last_log_id(pg, pipeline, stats.last_id)
                pg.commit()

        log(f"page {page} rows={len(rows)} max_log_id={stats.max_log_id}")
        detail(
            {
                "phase": "page_done",
                "total_rows": total_exchangelog,
                "loaded_rows": stats.fetched,
                "parsed_facts": stats.facts,
                "journal_facts": stats.facts,
                "staging_errors": stats.staging_n,
                "page": page,
            }
        )

        if len(rows) < batch:
            break

    if pg is not None and fact_buffer:
        upsert_facts_batch(pg, fact_buffer)
        pg.commit()
    if pg is not None and staging_buffer:
        insert_staging_errors(pg, staging_buffer)
        pg.commit()

    return stats


def _refresh_outbound_documents(
    cfg: CorpAppConfig,
    pg: Any,
    *,
    enrichment: EnrichmentCache,
    progress_state: dict[str, int],
    progress_detail_cb: Callable[[EtlProgressPayload], None] | None,
    log: Callable[[str], None],
    detail: Callable[[EtlProgressPayload], None],
) -> None:
    """Полная перезапись `stg_egisz_outbound_documents` снимком EGISZ_MESSAGES за окно ETL."""
    log("Refreshing stg_egisz_outbound_documents (v_rpt_documents_no_response)...")

    base_progress = {
        "total_rows": progress_state["total_exchangelog"],
        "loaded_rows": progress_state["fetched"],
        "parsed_facts": progress_state["facts"],
        "journal_facts": progress_state["facts"],
        "staging_errors": progress_state["staging_n"],
    }
    detail({"phase": "outbound_firebird", **base_progress})  # type: ignore[arg-type]

    omsg = fetch_all(cfg.firebird, outbound_documents_staging_select(cfg.etl.sync_window_days))
    omsg_sorted = sorted(
        omsg,
        key=lambda row: _sent_at_utc(row.get("msg_sent_at"))
        or datetime.min.replace(tzinfo=timezone.utc),
        reverse=True,
    )
    outbound_n = len(omsg_sorted)
    detail(
        {
            "phase": "outbound_fetch",
            "outbound_total": outbound_n,
            "outbound_loaded": 0,
            **base_progress,
        }  # type: ignore[arg-type]
    )

    parser_oob = EgiszMonitorParser()
    stg_out: list[dict[str, Any]] = []
    seen_doc: set[str] = set()
    for oi, r in enumerate(omsg_sorted, start=1):
        did = _to_str(r.get("documentid"))
        skip = not did or did in seen_doc
        if not skip:
            jid = _to_int(r.get("egisz_licenses_jid"))
            jn = enrichment.jname_by_jid.get(jid) if jid else None
            if _is_test_clinic(jn):
                skip = True
        if not skip:
            seen_doc.add(did)  # type: ignore[arg-type]
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
                    **base_progress,
                }  # type: ignore[arg-type]
            )

    og_total = len(stg_out)
    detail(
        {
            "phase": "outbound_postgres",
            "outbound_total": og_total,
            "outbound_loaded": 0,
            "parsed_facts": og_total,
            **base_progress,
        }  # type: ignore[arg-type]
    )
    refresh_outbound_documents_staging(pg, stg_out)
    pg.commit()
    detail(
        {
            "phase": "outbound_done",
            "outbound_total": og_total,
            "outbound_loaded": og_total,
            "parsed_facts": og_total,
            **base_progress,
        }  # type: ignore[arg-type]
    )


def _read_cursor(cfg: CorpAppConfig, pg: Any, *, dry_run: bool, full_scan: bool) -> int:
    """Прочитать `etl_state.last_log_id` (для dry-run открыть отдельное соединение к PG)."""
    if full_scan:
        return 0
    pipeline = cfg.etl.pipeline_name
    if dry_run:
        pg_r = connect_pg(cfg.postgres)
        try:
            ensure_etl_state_table(pg_r)
            return get_last_log_id(pg_r, pipeline)
        finally:
            pg_r.close()
    if pg is not None:
        return get_last_log_id(pg, pipeline)
    return 0


def run_sync(
    cfg: CorpAppConfig | None = None,
    *,
    dry_run: bool = False,
    progress_cb: Callable[[str], None] | None = None,
    progress_detail_cb: Callable[[EtlProgressPayload], None] | None = None,
) -> EtlRunStats:
    """Load enrichment from Firebird, paginate EXCHANGELOG by LOGID, parse MSGTEXT (SOAP) + LOGTEXT (host), UPSERT PG."""
    cfg = cfg or load_corp_config()

    def log(msg: str) -> None:
        if progress_cb:
            progress_cb(msg)

    def detail(payload: EtlProgressPayload) -> None:
        if progress_detail_cb:
            progress_detail_cb(payload)

    base_sql = (cfg.etl.source_query or "").strip() or default_exchangelog_select(
        cfg.etl.sync_window_days
    )
    pipeline = cfg.etl.pipeline_name

    enrichment = _load_enrichment_cache(cfg, log)

    pg = None if dry_run else connect_pg(cfg.postgres)
    lock_acquired = False

    try:
        if pg is not None:
            apply_sql_files(pg, "001_schema.sql", "002_etl_state.sql", "005_healthcheck.sql")
            ensure_etl_state_table(pg)
            # Single-flight на уровне БД: блокирует параллельный запуск из CronJob и UI.
            # Lock освобождается при close() соединения, поэтому крэш не оставит «навечно занято».
            lock_acquired = try_acquire_pipeline_lock(pg, pipeline)
            if not lock_acquired:
                raise PipelineLockBusyError(
                    f"Sync пайплайна '{pipeline}' уже выполняется (advisory lock занят). "
                    "Дождитесь завершения текущего запуска или проверьте `pg_locks`."
                )

        last_id = _read_cursor(cfg, pg, dry_run=dry_run, full_scan=cfg.etl.full_scan)
        log(f"ETL cursor last_log_id={last_id} pipeline={pipeline} full_scan={cfg.etl.full_scan}")

        detail({"phase": "counting"})
        log("Подсчёт строк EXCHANGELOG для прогресса...")
        total_exchangelog = _count_exchangelog_total(
            cfg,
            base_sql,
            has_custom_query=bool((cfg.etl.source_query or "").strip()),
            last_id=last_id,
            log=log,
        )
        log(f"EXCHANGELOG к обработке (LOGID > {last_id}): {total_exchangelog} строк.")
        detail(
            {
                "phase": "exchangelog_ready",
                "total_rows": total_exchangelog,
                "loaded_rows": 0,
                "parsed_facts": 0,
                "journal_facts": 0,
                "staging_errors": 0,
            }
        )

        page_stats = _process_exchangelog_pages(
            cfg,
            pg,
            base_sql=base_sql,
            enrichment=enrichment,
            last_id=last_id,
            total_exchangelog=total_exchangelog,
            progress_detail_cb=progress_detail_cb,
            log=log,
            detail=detail,
        )

        detail(
            {
                "phase": "exchangelog_done",
                "total_rows": total_exchangelog,
                "loaded_rows": page_stats.fetched,
                "parsed_facts": page_stats.facts,
                "journal_facts": page_stats.facts,
                "staging_errors": page_stats.staging_n,
            }
        )

        if pg is not None and not dry_run:
            _refresh_outbound_documents(
                cfg,
                pg,
                enrichment=enrichment,
                progress_state={
                    "total_exchangelog": total_exchangelog,
                    "fetched": page_stats.fetched,
                    "facts": page_stats.facts,
                    "staging_n": page_stats.staging_n,
                },
                progress_detail_cb=progress_detail_cb,
                log=log,
                detail=detail,
            )

        cursor_after = (
            get_last_log_id(pg, pipeline) if pg is not None else page_stats.last_id
        )
        return EtlRunStats(
            fetched=page_stats.fetched,
            facts_upserted=page_stats.facts,
            staging_errors=page_stats.staging_n,
            max_log_id=page_stats.max_log_id,
            last_cursor_after=cursor_after,
        )
    finally:
        if pg is not None:
            if lock_acquired:
                release_pipeline_lock(pg, pipeline)
            pg.close()
