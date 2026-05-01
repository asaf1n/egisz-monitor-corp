"""Firebird → PostgreSQL ETL for corp fact table (LOGID cursor, not MODIFYDATE).

Архитектура `run_sync` (сначала выгрузка из FB, затем парсинг/UPSERT):
  1. `_export_egisz_licenses_full` — полная выгрузка EGISZ_LICENSES + JOIN JPERSONS из Firebird; отбор по `sync_window_days` по полю MODIFYDATE выполняется в Python.
  2. `_count_exchangelog_total` — оценка объёма журнала после LOGID (только Firebird, до тяжёлой выгрузки сообщений).
  3. `_export_egisz_messages_by_egmid` — COUNT EGISZ_MESSAGES в окне (прогресс UI: `messages_counting`), затем постраничная выгрузка по EGMID; в PG сразу обновляется
     `etl_state.source_max_egmid` (пик выгрузки для UI); `last_egmid` — только после полного успешного sync.
  4. `_process_exchangelog_pages` — для каждой страницы: выборка EXCHANGELOG из FB по LOGID, затем в Python склейка
     по MSGID с выгруженными сообщениями, парсинг, UPSERT (выгрузка журнала и разбор разнесены по шагам внутри цикла).
  5. `_refresh_outbound_documents` — полная перезапись `stg_egisz_outbound_documents` из Firebird и запись в PostgreSQL.

Расщепление сделано для тестируемости (`tests/test_etl_*`) и читаемости логов: каждая
сабфаза публикует своё имя в `EtlProgressPayload.phase`, UI отображает русскую подпись.

Безопасность параллельного запуска: `pg_try_advisory_lock(hash(pipeline))` — CronJob и
UI-кнопка теперь не могут стартовать sync одновременно, выполнится только первый.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Sequence, TypedDict

from egisz_monitor_corp.config_loader import CorpAppConfig, load_corp_config
from egisz_monitor_corp.fb_client import fetch_all
from egisz_monitor_corp.parser import EgiszMonitorParser, StagingParseError, _norm_kind_code
from egisz_monitor_corp.pg_warehouse import (
    PipelineLockBusyError,
    apply_sql_files,
    connect_pg,
    ensure_etl_state_table,
    get_last_egmid,
    get_last_log_id,
    insert_staging_errors,
    refresh_outbound_documents_staging,
    release_pipeline_lock,
    set_etl_source_peaks,
    set_last_egmid,
    set_last_log_id,
    try_acquire_pipeline_lock,
    upsert_dim_clinic,
    upsert_dim_semd,
    upsert_facts_batch,
)
from egisz_monitor_corp.sql_util import (
    default_exchangelog_select,
    egisz_messages_count_sql,
    egisz_messages_incremental_sql,
    enrichment_egisz_licenses_sql,
    exchangelog_count_after_cursor,
    exchangelog_count_logid_after_cursor,
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


@dataclass(frozen=True)
class LicenseReplyRow:
    """Строка лицензии с MO_DOMEN для сопоставления с REPLYTO (подстрока домена в адресе)."""

    mo_domen: str
    modifydate: datetime | None
    lic_id: int
    jid: int | None
    kind: Any
    mo_uid: str | None


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
    messages_cursor_egmid: int


@dataclass
class EnrichmentCache:
    """Справочники Firebird, загруженные в начале run_sync (O(1) lookups в горячем цикле)."""

    mo_uid_to_jid_from_egisz_licenses: dict[str, int] = field(default_factory=dict)
    # Первая строка EGISZ_LICENSES по JID (порядок как в clinics) — для upsert_dim_clinic без O(n) по списку.
    clinic_dim_by_jid: dict[int, tuple[str | None, str | None, str | None]] = field(default_factory=dict)
    clinics: list[tuple[int, str | None, str | None, str | None, str | None]] = field(
        default_factory=list
    )
    jname_by_jid: dict[int, str] = field(default_factory=dict)
    jpersons_by_jid: dict[int, tuple[str | None, str | None, str | None]] = field(
        default_factory=dict
    )
    license_reply_rows: tuple[LicenseReplyRow, ...] = ()
    max_licenses_modifydate: datetime | None = None


def _to_int(v: Any) -> int | None:
    if v is None:
        return None
    try:
        n = int(v)
        return n if n > 0 else None
    except (TypeError, ValueError):
        return None


def _egmid_sql_int(v: Any) -> int | None:
    """Целый EGMID из Firebird для продвижения курсора (0 допустим; иначе _to_int отбрасывает 0 и ломает цикл)."""
    if v is None:
        return None
    try:
        return int(v)
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


def _norm_msgid_key(v: Any) -> str | None:
    if v is None:
        return None
    s = str(v).strip()
    return s or None


def _license_for_reply_to(
    reply_to: str | None, rows: Sequence[LicenseReplyRow]
) -> LicenseReplyRow | None:
    """Эквивалент FB: POSITION(TRIM(MO_DOMEN) IN TRIM(REPLYTO)) с выбором FIRST 1 ORDER BY MODIFYDATE DESC, ID DESC."""
    if not reply_to or not rows:
        return None
    rt = str(reply_to).strip()
    if not rt:
        return None
    candidates: list[LicenseReplyRow] = []
    for row in rows:
        dom = row.mo_domen
        if dom and dom in rt:
            candidates.append(row)
    if not candidates:
        return None

    def sort_key(row: LicenseReplyRow) -> tuple[datetime, int]:
        md = row.modifydate
        if md is not None and md.tzinfo is not None:
            md = md.replace(tzinfo=None)
        base = md or datetime.min
        return (base, row.lic_id)

    return max(candidates, key=sort_key)


def _is_test_clinic(jname: str | None) -> bool:
    """Тестовые клиники: jname содержит 'test' или 'тест' (в нижнем регистре). Решение зафиксировано в .cursorrules."""
    if not jname:
        return False
    low = jname.lower()
    return "test" in low or "тест" in low


def _license_modifydate_in_window(md_raw: Any, sync_window_days: int) -> bool:
    """Строка лицензии участвует в кэше, если MODIFYDATE в окне sync_window_days (как раньше в SQL) либо дата не задана."""
    if sync_window_days <= 0:
        return True
    if md_raw is None:
        return True
    md_utc = _sent_at_utc(md_raw)
    if md_utc is None:
        return True
    cutoff = datetime.now(timezone.utc) - timedelta(days=int(sync_window_days))
    return md_utc >= cutoff


def _export_egisz_licenses_full(cfg: CorpAppConfig, log: Callable[[str], None]) -> EnrichmentCache:
    """Полная выгрузка EGISZ_LICENSES из Firebird с LEFT JOIN JPERSONS; отбор по MODIFYDATE в окне sync_window_days — в Python."""
    log("Firebird: выгрузка EGISZ_LICENSES (JOIN JPERSONS), фильтр по MODIFYDATE в процессе…")
    sd = cfg.etl.sync_window_days
    all_lic = fetch_all(cfg.firebird, enrichment_egisz_licenses_sql())
    egisz_licenses_rows = [r for r in all_lic if _license_modifydate_in_window(r.get("modifydate"), sd)]
    if len(all_lic) != len(egisz_licenses_rows):
        log(
            f"EGISZ_LICENSES: прочитано {len(all_lic)} строк, в окне MODIFYDATE ({sd} сут.): {len(egisz_licenses_rows)}"
        )

    cache = EnrichmentCache()
    reply_rows: list[LicenseReplyRow] = []
    max_lic_md: datetime | None = None
    for r in egisz_licenses_rows:
        mo = _to_str(r.get("mo_uid"))
        jid = _to_int(r.get("jid"))
        jn = _to_str(r.get("jname"))
        if jid:
            jinn_v = _to_str(r.get("jinn"))
            fir_v = _to_str(r.get("fir_oid"))
            prev = cache.jpersons_by_jid.get(jid)
            if prev is None or (jn and not prev[0]):
                cache.jpersons_by_jid[jid] = (jn, jinn_v, fir_v)
            if jn:
                cache.jname_by_jid[jid] = jn
        md_raw = r.get("modifydate")
        if isinstance(md_raw, datetime):
            if max_lic_md is None or md_raw > max_lic_md:
                max_lic_md = md_raw
        dom_raw = _to_str(r.get("mo_domen"))
        if dom_raw:
            md = r.get("modifydate")
            md_dt = md if isinstance(md, datetime) else None
            lic_id = _to_int(r.get("id")) or 0
            reply_rows.append(
                LicenseReplyRow(
                    mo_domen=dom_raw.strip(),
                    modifydate=md_dt,
                    lic_id=lic_id,
                    jid=jid,
                    kind=r.get("egisz_licenses_kind"),
                    mo_uid=mo,
                )
            )
        if mo and jid:
            cache.mo_uid_to_jid_from_egisz_licenses[mo] = jid
        if jid:
            jinn_row = _to_str(r.get("jinn"))
            fir_row = _to_str(r.get("fir_oid"))
            if jid not in cache.clinic_dim_by_jid:
                cache.clinic_dim_by_jid[jid] = (jn, jinn_row, fir_row)
            cache.clinics.append(
                (
                    jid,
                    jn,
                    mo,
                    jinn_row,
                    fir_row,
                )
            )
            if jn and jid not in cache.jname_by_jid:
                cache.jname_by_jid[jid] = jn
    cache.license_reply_rows = tuple(reply_rows)
    cache.max_licenses_modifydate = max_lic_md
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
            cnt_sql = exchangelog_count_logid_after_cursor(last_log_id=last_id)
        cnt_rows = fetch_all(cfg.firebird, cnt_sql)
        if cnt_rows:
            raw = cnt_rows[0].get("cnt")
            if raw is not None:
                return int(raw)
    except Exception as ex:  # pragma: no cover - сеть/FB
        log(f"Предупреждение: не удалось получить COUNT для прогресса ({ex}).")
    return 0


def _count_egisz_messages_window(
    cfg: CorpAppConfig, last_egmid: int, *, log: Callable[[str], None]
) -> int:
    """COUNT EGISZ_MESSAGES с теми же условиями, что постраничная выгрузка (для total_rows в UI)."""
    try:
        sql = egisz_messages_count_sql(
            last_egmid=last_egmid, sync_window_days=cfg.etl.sync_window_days
        )
        cnt_rows = fetch_all(cfg.firebird, sql)
        if cnt_rows:
            raw = cnt_rows[0].get("cnt")
            if raw is not None:
                return int(raw)
    except Exception as ex:  # pragma: no cover - сеть/FB
        log(f"Предупреждение: не удалось COUNT для EGISZ_MESSAGES ({ex}).")
    return 0


def _export_egisz_messages_by_egmid(
    cfg: CorpAppConfig,
    last_egmid: int,
    *,
    log: Callable[[str], None],
    detail: Callable[[EtlProgressPayload], None] | None = None,
) -> tuple[dict[str, dict[str, Any]], int]:
    """Выгрузка из Firebird: EGISZ_MESSAGES страницами с EGMID выше курсора и CREATEDATE в окне sync_window_days."""
    batch = max(1, cfg.etl.batch_size)
    if detail is not None:
        detail(
            {
                "phase": "messages_counting",
                "loaded_rows": 0,
                "total_rows": 0,
                "page": 0,
                "parsed_facts": 0,
                "journal_facts": 0,
                "staging_errors": 0,
                "messages_cursor_egmid": int(last_egmid),
            }
        )
    log(
        "Подсчёт строк EGISZ_MESSAGES в Firebird для прогресса (COUNT по окну sync_window_days; на большой базе может занять время)…"
    )
    msg_total = _count_egisz_messages_window(cfg, last_egmid, log=log)
    msg_by_msgid: dict[str, dict[str, Any]] = {}
    cursor = int(last_egmid)
    total = 0
    page_n = 0
    if detail is not None:
        detail(
            {
                "phase": "messages_incremental",
                "loaded_rows": 0,
                "total_rows": msg_total,
                "page": 0,
                "parsed_facts": 0,
                "journal_facts": 0,
                "staging_errors": 0,
                "messages_cursor_egmid": cursor,
            }
        )
    while True:
        page_n += 1
        prev_cursor = cursor
        sql = egisz_messages_incremental_sql(
            last_egmid=cursor,
            limit=batch,
            sync_window_days=cfg.etl.sync_window_days,
        )
        rows = fetch_all(cfg.firebird, sql)
        if not rows:
            break
        total += len(rows)
        page_max = cursor
        for r in rows:
            eg = _egmid_sql_int(r.get("egmid"))
            if eg is not None:
                page_max = max(page_max, eg)
            mk = _norm_msgid_key(r.get("msgid"))
            if mk:
                msg_by_msgid[mk] = r
        cursor = page_max
        if detail is not None:
            detail(
                {
                    "phase": "messages_incremental",
                    "loaded_rows": total,
                    "total_rows": msg_total,
                    "page": page_n,
                    "parsed_facts": 0,
                    "journal_facts": 0,
                    "staging_errors": 0,
                    "messages_cursor_egmid": cursor,
                }
            )
        if len(rows) >= batch and cursor == prev_cursor:
            log(
                "Предупреждение: EGISZ_MESSAGES — полная страница, но курсор EGMID не сдвинулся; "
                "останавливаем загрузку (проверьте EGMID в Firebird и charset)."
            )
            break
        if page_n == 1 or page_n % 20 == 0:
            log(f"EGISZ_MESSAGES: страница {page_n}, всего строк {total}, курсор EGMID={cursor}")
        if len(rows) < batch:
            break
    log(
        f"EGISZ_MESSAGES: загружено {total} строк после EGMID={last_egmid}, "
        f"уникальных MSGID={len(msg_by_msgid)}, курсор EGMID={cursor}"
    )
    return msg_by_msgid, cursor


def _export_exchangelog_page(
    cfg: CorpAppConfig,
    base_sql: str,
    *,
    last_log_id: int,
    limit: int,
) -> list[dict[str, Any]]:
    """Одна страница EXCHANGELOG из Firebird по LOGID (только SELECT, без парсинга)."""
    sql = paginated_exchangelog_sql(base_sql, last_log_id=last_log_id, limit=limit)
    return fetch_all(cfg.firebird, sql)


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
    msg_by_msgid: dict[str, dict[str, Any]],
    last_id: int,
    total_exchangelog: int,
    progress_detail_cb: Callable[[EtlProgressPayload], None] | None,
    log: Callable[[str], None],
    detail: Callable[[EtlProgressPayload], None],
) -> _PageStats:
    """Постранично: выгрузка EXCHANGELOG из Firebird по LOGID, затем склейка с выгруженными EGISZ_MESSAGES по MSGID, парсинг, UPSERT."""
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
        detail(
            {
                "phase": "exchangelog_export",
                "total_rows": total_exchangelog,
                "loaded_rows": stats.fetched,
                "parsed_facts": stats.facts,
                "journal_facts": stats.facts,
                "staging_errors": stats.staging_n,
                "page": page,
            }
        )
        rows = _export_exchangelog_page(
            cfg, base_sql, last_log_id=stats.last_id, limit=batch
        )
        if not rows:
            break
        stats.fetched += len(rows)

        detail(
            {
                "phase": "exchangelog_parse",
                "total_rows": total_exchangelog,
                "loaded_rows": stats.fetched,
                "parsed_facts": stats.facts,
                "journal_facts": stats.facts,
                "staging_errors": stats.staging_n,
                "page": page,
            }
        )

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

            mk = _norm_msgid_key(r.get("msgid"))
            mrow = msg_by_msgid.get(mk) if mk else None
            if mrow:
                reply_to = _to_str(mrow.get("replyto"))
                doc_id = _to_str(mrow.get("documentid"))
                msg_created = _sent_at_utc(mrow.get("msg_created_at"))
            else:
                reply_to = doc_id = None
                msg_created = None

            lic = _license_for_reply_to(reply_to, enrichment.license_reply_rows)
            egisz_licenses_kind = lic.kind if lic else None
            mo_uid = lic.mo_uid if lic else None
            egisz_licenses_jid = lic.jid if lic else None

            log_created = _sent_at_utc(r.get("log_created_at"))
            lim = cfg.etl.max_msgtext_bytes
            if lim is not None and lim > 0 and msgtext:
                nbytes = len(msgtext.encode("utf-8", errors="replace"))
                if nbytes > lim:
                    combined_ex = "\n".join(
                        x for x in ((msgtext or "").strip(), (logtext or "").strip()) if x
                    )
                    cap = parser.log_excerpt_max
                    excerpt = (
                        (combined_ex[:cap] + "…") if len(combined_ex) > cap else (combined_ex or None)
                    )
                    on_stage(
                        StagingParseError(
                            relates_to_id=None,
                            error_code="MSGTEXT_TOO_LARGE",
                            message=f"MSGTEXT UTF-8 size {nbytes} exceeds max_msgtext_bytes={lim}",
                            log_excerpt=excerpt,
                        )
                    )
                    continue

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
                dim_row = enrichment.clinic_dim_by_jid.get(rec.jid)
                if dim_row:
                    jn_dim, jinn_v, fir_v = dim_row
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
    """Полная перезапись `stg_egisz_outbound_documents`: FB только DOCUMENTID/EGMID/даты/REPLYTO; KIND/JID — в Python."""
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
    # SQL уже ORDER BY m.EGMID DESC: при монотонном EGMID первая строка на DOCUMENTID — самая новая.
    omsg_sorted = omsg
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
        reply_to = _to_str(r.get("replyto"))
        lic = _license_for_reply_to(reply_to, enrichment.license_reply_rows)
        jid = lic.jid if lic else None
        if not skip:
            jn = enrichment.jname_by_jid.get(jid) if jid else None
            if _is_test_clinic(jn):
                skip = True
        if not skip:
            seen_doc.add(did)  # type: ignore[arg-type]
            host_part = parser_oob.extract_jid(None, reply_to=reply_to)
            kraw = lic.kind if lic else None
            kc = _norm_kind_code(str(kraw).strip() if kraw is not None else None)
            stg_out.append(
                {
                    "document_id": did,
                    "sent_at": _sent_at_utc(r.get("msg_sent_at")),
                    "reply_to": reply_to,
                    "gost_jid_token": host_part.get("gost_jid_token"),
                    "kind_code": kc,
                    "jid": jid,
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


def _read_egmid_cursor(cfg: CorpAppConfig, pg: Any, *, dry_run: bool, full_scan: bool) -> int:
    """Прочитать `etl_state.last_egmid` (курсор EGISZ_MESSAGES)."""
    if full_scan:
        return 0
    pipeline = cfg.etl.pipeline_name
    if dry_run:
        pg_r = connect_pg(cfg.postgres)
        try:
            ensure_etl_state_table(pg_r)
            return get_last_egmid(pg_r, pipeline)
        finally:
            pg_r.close()
    if pg is not None:
        return get_last_egmid(pg, pipeline)
    return 0


def run_sync(
    cfg: CorpAppConfig | None = None,
    *,
    dry_run: bool = False,
    progress_cb: Callable[[str], None] | None = None,
    progress_detail_cb: Callable[[EtlProgressPayload], None] | None = None,
) -> EtlRunStats:
    """Оркестрация Firebird → Postgres: полная выгрузка лицензий; оценка журнала по LOGID; выгрузка сообщений по EGMID
    (пик в source_max_egmid сразу; last_egmid в конце успешного прогона); парсинг/UPSERT EXCHANGELOG с сопоставлением MSGID в памяти."""
    cfg = cfg or load_corp_config()

    def log(msg: str) -> None:
        if progress_cb:
            progress_cb(msg)

    def detail(payload: EtlProgressPayload) -> None:
        if progress_detail_cb:
            progress_detail_cb(payload)

    base_sql = (cfg.etl.source_query or "").strip() or default_exchangelog_select()
    pipeline = cfg.etl.pipeline_name

    detail({"phase": "enrichment_firebird"})
    enrichment = _export_egisz_licenses_full(cfg, log)

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
            # MAX(MODIFYDATE) лицензий уже в памяти — пишем в etl_state до долгих запросов к FB (UI / healthcheck).
            if not dry_run and enrichment.max_licenses_modifydate is not None:
                set_etl_source_peaks(pg, pipeline, None, enrichment.max_licenses_modifydate)
                pg.commit()

        last_id = _read_cursor(cfg, pg, dry_run=dry_run, full_scan=cfg.etl.full_scan)
        last_egmid = _read_egmid_cursor(cfg, pg, dry_run=dry_run, full_scan=cfg.etl.full_scan)
        log(
            f"ETL cursor last_log_id={last_id} last_egmid={last_egmid} "
            f"pipeline={pipeline} full_scan={cfg.etl.full_scan}"
        )

        detail({"phase": "counting"})
        log(
            "Подсчёт строк EXCHANGELOG в Firebird для прогресса (COUNT может занять несколько минут на большой базе)…"
        )
        total_exchangelog = _count_exchangelog_total(
            cfg,
            base_sql,
            has_custom_query=bool((cfg.etl.source_query or "").strip()),
            last_id=last_id,
            log=log,
        )
        log(f"EXCHANGELOG к обработке (LOGID > {last_id}): {total_exchangelog} строк.")

        msg_by_msgid, egmid_after_export = _export_egisz_messages_by_egmid(
            cfg, last_egmid, log=log, detail=detail
        )

        if pg is not None and not dry_run and egmid_after_export > last_egmid:
            set_etl_source_peaks(pg, pipeline, int(egmid_after_export), None)
            pg.commit()
            log(
                f"Пик выгрузки EGISZ_MESSAGES записан в etl_state.source_max_egmid={egmid_after_export} "
                "(полный курсор last_egmid обновится после успешного завершения журнала и outbound)."
            )

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
            msg_by_msgid=msg_by_msgid,
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
            eg_peak = egmid_after_export if egmid_after_export > 0 else None
            set_etl_source_peaks(
                pg,
                pipeline,
                eg_peak,
                enrichment.max_licenses_modifydate,
            )
            if egmid_after_export > last_egmid:
                set_last_egmid(pg, pipeline, egmid_after_export)
            pg.commit()

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
