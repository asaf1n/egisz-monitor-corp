"""Firebird → PostgreSQL ETL for corp fact table (LOGID cursor, not MODIFYDATE).

Источники Firebird в `run_sync` (четыре таблицы, затем витрина):
  • Полная перезагрузка в staging: **JPERSONS**, **EGISZ_LICENSES** (`_export_egisz_licenses_full`).
  • Инкремент по ключу: **EGISZ_MESSAGES** (курсор EGMID), **EXCHANGELOG** (курсор LOGID) — чередование
    пакетов по 65k, дозагрузка MSGID, парсинг и UPSERT фактов.
  • Дополнительно: `_refresh_outbound_documents` — снимок исходящих по EGMID.

Архитектура (сначала выгрузка из FB, затем парсинг/UPSERT):
  1. `_export_egisz_licenses_full` — JPERSONS и EGISZ_LICENSES отдельными SELECT из FB; в PostgreSQL — `stg_jpersons_import`, `stg_egisz_licenses_import`, сшивка JNAME/JINN/FIR_OID в SQL (`UPDATE … FROM`), staging, merge в `dim_clinics`.
  2. `_count_exchangelog_total` — заглушка (без COUNT в Firebird); в payload UI поле `total_rows` не передаётся, пока объём неизвестен.
  3. Чередование пакетов по 65k: EGISZ_MESSAGES (EGMID) и EXCHANGELOG (LOGID); дозагрузка сообщений по MSGID строки журнала;
     пока парсится/пишется страница журнала, в фоне запрашивается следующая страница сообщений из Firebird.
  4. `_process_exchangelog_pages` — полный проход журнала (тесты); в `run_sync` используется потоковое чередование.
  5. `_refresh_outbound_documents` — полная перезапись `stg_egisz_outbound_documents` из Firebird и запись в PostgreSQL.

Расщепление сделано для тестируемости (`tests/test_etl_*`) и читаемости логов: фазы в
`EtlProgressPayload.phase` для UI; частые обновления одной и той же фазы троттлятся в `run_sync`, смена фазы — сразу.

Безопасность параллельного запуска: `pg_try_advisory_lock(hash(pipeline))` — CronJob и
UI-кнопка теперь не могут стартовать sync одновременно, выполнится только первый.
"""

from __future__ import annotations

import time
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Mapping, Sequence, TypedDict

from egisz_monitor_corp.config_loader import CorpAppConfig, load_corp_config
from egisz_monitor_corp.fb_client import fetch_all
from egisz_monitor_corp.parser import EgiszMonitorParser, StagingParseError, _norm_kind_code
from egisz_monitor_corp.pg_warehouse import (
    PipelineLockBusyError,
    apply_reports_schema,
    connect_pg,
    ensure_etl_state_table,
    fetch_license_rows_for_enrichment,
    get_last_egmid,
    get_last_log_id,
    insert_staging_errors,
    merge_dim_clinics_from_license_staging,
    refresh_license_staging_from_firebird_exports,
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
    egisz_messages_by_msgids_sql,
    egisz_messages_incremental_sql,
    enrichment_egisz_licenses_only_sql,
    jpersons_all_sql,
    outbound_documents_staging_select,
    paginated_exchangelog_sql,
)


# Минимальный интервал между одинаковыми фазами в progress_detail_cb (сек.): UI не должен замедлять ETL.
_DETAIL_THROTTLE_SEC = 0.22

# Чередование EGISZ_MESSAGES / EXCHANGELOG: размер страницы из Firebird (FIRST n).
_INTERLEAVE_PAGE_ROWS = 65_000
_MSGBACKFILL_IN_CHUNK = 180


def _etl_fb_fetch(
    cfg: CorpAppConfig,
    sql: str,
    params: Sequence[Any] | Mapping[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """SELECT к Firebird с таймаутом из конфига (COUNT и постраничные выгрузки)."""
    return fetch_all(
        cfg.firebird,
        sql,
        params,
        timeout_sec=cfg.etl.firebird_query_timeout_sec,
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
    """Снимок прогресса для UI (все поля опциональны кроме phase).

    phase: pipeline_bootstrap | enrichment_firebird | counting | messages_* | exchangelog_* | outbound_* | …
    """

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
    messages_batch_rows: int
    messages_msgid_cache_size: int
    cursor_log_id: int
    licenses_modifydate_iso: str


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


def _fb_sql_bigint(v: Any) -> int | None:
    """Целое значение из Firebird для хранения в BIGINT (в т.ч. LOGID, может быть 0)."""
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


def _clean_license_rows_after_firebird(raw: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Строки с валидным JID (как отбор в fetch_license_rows_for_enrichment в PostgreSQL)."""
    return [r for r in raw if _to_int(r.get("jid")) is not None]


def _jpersons_map_from_firebird_rows(jp_rows: list[dict[str, Any]]) -> dict[int, tuple[str | None, str | None, str | None]]:
    m: dict[int, tuple[str | None, str | None, str | None]] = {}
    for r in jp_rows:
        jid = _to_int(r.get("jid"))
        if not jid:
            continue
        m[jid] = (_to_str(r.get("jname")), _to_str(r.get("jinn")), _to_str(r.get("fir_oid")))
    return m


def _license_rows_with_jpersons(
    lic_rows: list[dict[str, Any]], jp_by_jid: dict[int, tuple[str | None, str | None, str | None]]
) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for r in lic_rows:
        rr = dict(r)
        jid = _to_int(rr.get("jid"))
        jn, jinn, fir = jp_by_jid.get(jid, (None, None, None)) if jid else (None, None, None)
        rr["jname"] = jn
        rr["jinn"] = jinn
        rr["fir_oid"] = fir
        out.append(rr)
    return out


def _fetch_jpersons_and_licenses_separate(
    cfg: CorpAppConfig, log: Callable[[str], None]
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    log("Firebird [1/2]: выгрузка JPERSONS (все JID)…")
    jp_rows = _etl_fb_fetch(cfg, jpersons_all_sql())
    log(f"JPERSONS: прочитано {len(jp_rows)} строк из Firebird.")
    log("Firebird [2/2]: выгрузка EGISZ_LICENSES (без JOIN)…")
    lic_rows = _etl_fb_fetch(cfg, enrichment_egisz_licenses_only_sql())
    log(f"EGISZ_LICENSES: прочитано {len(lic_rows)} строк из Firebird.")
    return jp_rows, lic_rows


def _fetch_egisz_licenses_raw_from_firebird(
    cfg: CorpAppConfig, log: Callable[[str], None]
) -> list[dict[str, Any]]:
    """Сшивка JPERSONS + лицензии в Python (режим без PostgreSQL, например dry-run)."""
    jp_rows, lic_rows = _fetch_jpersons_and_licenses_separate(cfg, log)
    merged = _license_rows_with_jpersons(lic_rows, _jpersons_map_from_firebird_rows(jp_rows))
    log(f"EGISZ_LICENSES: сшивка с JPERSONS в Python → {len(merged)} строк.")
    return merged


def _upsert_facts_from_buffer(pg: Any, fact_buffer: list[dict[str, Any]], cfg: CorpAppConfig) -> None:
    upsert_facts_batch(
        pg,
        fact_buffer,
        chunk_size=cfg.etl.facts_upsert_chunk_size,
        commit_each_chunk=True,
        statement_timeout_sec=cfg.etl.pg_upsert_statement_timeout_sec,
    )


def _build_enrichment_cache_from_license_rows(
    egisz_licenses_rows: list[dict[str, Any]],
) -> EnrichmentCache:
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


def _export_egisz_licenses_full(
    cfg: CorpAppConfig,
    log: Callable[[str], None],
    *,
    pg: Any | None = None,
) -> EnrichmentCache:
    """Полная выгрузка JPERSONS и EGISZ_LICENSES с Firebird; при PG — staging, сшивка JNAME/JINN/FIR_OID в SQL, merge dim_clinics."""
    jp_rows, lic_rows = _fetch_jpersons_and_licenses_separate(cfg, log)
    if pg is not None:
        log(
            "PostgreSQL: staging JPERSONS + лицензий, сшивка полей юрлица в SQL (UPDATE … FROM), merge dim_clinics…"
        )
        insert_chunk = max(1000, min(cfg.etl.facts_upsert_chunk_size * 4, 20_000))
        refresh_license_staging_from_firebird_exports(
            pg,
            jpersons_rows=jp_rows,
            license_rows=lic_rows,
            insert_chunk_size=insert_chunk,
        )
        merge_dim_clinics_from_license_staging(pg)
        rows = fetch_license_rows_for_enrichment(pg)
        pg.commit()
    else:
        raw = _license_rows_with_jpersons(lic_rows, _jpersons_map_from_firebird_rows(jp_rows))
        rows = _clean_license_rows_after_firebird(raw)
        dropped = len(raw) - len(rows)
        if dropped:
            log(
                f"EGISZ_LICENSES: отброшено {dropped} строк без JID (режим без записи в PostgreSQL)."
            )
    return _build_enrichment_cache_from_license_rows(rows)


def _count_exchangelog_total() -> int:
    """Заглушка: COUNT по журналу в Firebird не выполняется (дорого на больших базах)."""
    return 0


def _progress_payload_total_rows(total: int) -> dict[str, int]:
    """В JSON не кладём total_rows=0: в UI это смешивали с «объём неизвестен»."""
    if total > 0:
        return {"total_rows": int(total)}
    return {}


@dataclass
class _PageStats:
    fetched: int = 0
    facts: int = 0
    staging_n: int = 0
    max_log_id: int = 0
    last_id: int = 0


def _messages_page_after_egmid(
    cfg: CorpAppConfig, cursor: int, limit: int
) -> tuple[list[dict[str, Any]], int, int]:
    """Одна страница EGISZ_MESSAGES по EGMID. Возвращает (rows, new_cursor, prev_cursor)."""
    prev = int(cursor)
    sql = egisz_messages_incremental_sql(last_egmid=cursor, limit=limit)
    rows = _etl_fb_fetch(cfg, sql)
    if not rows:
        return rows, prev, prev
    page_max = prev
    for r in rows:
        eg = _egmid_sql_int(r.get("egmid"))
        if eg is not None:
            page_max = max(page_max, eg)
    return rows, page_max, prev


def _merge_messages_rows_into_map(
    rows: list[dict[str, Any]], msg_by_msgid: dict[str, dict[str, Any]]
) -> None:
    for r in rows:
        mk = _norm_msgid_key(r.get("msgid"))
        if mk:
            msg_by_msgid[mk] = r


def _backfill_messages_for_msgids(
    cfg: CorpAppConfig,
    msgids: list[str],
    msg_by_msgid: dict[str, dict[str, Any]],
    log: Callable[[str], None],
) -> None:
    need = [m for m in msgids if m and m not in msg_by_msgid]
    if not need:
        return
    n = 0
    for i in range(0, len(need), _MSGBACKFILL_IN_CHUNK):
        chunk = need[i : i + _MSGBACKFILL_IN_CHUNK]
        ph = ",".join("?" * len(chunk))
        sql = egisz_messages_by_msgids_sql(ph)
        got = _etl_fb_fetch(cfg, sql, tuple(chunk))
        _merge_messages_rows_into_map(got, msg_by_msgid)
        n += len(got)
    if n:
        log(f"EGISZ_MESSAGES: дозагрузка по MSGID журнала — {n} строк, уникальных MSGID в кэше {len(msg_by_msgid)}.")


def _collect_missing_msgids_for_journal_rows(
    rows: list[dict[str, Any]], msg_by_msgid: dict[str, dict[str, Any]]
) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for r in rows:
        mk = _norm_msgid_key(r.get("msgid"))
        if mk and mk not in msg_by_msgid and mk not in seen:
            seen.add(mk)
            out.append(mk)
    return out


def _ingest_exchangelog_rows_chunk(
    cfg: CorpAppConfig,
    pg: Any,
    *,
    rows: list[dict[str, Any]],
    page: int,
    enrichment: EnrichmentCache,
    msg_by_msgid: dict[str, dict[str, Any]],
    total_exchangelog: int,
    parser: EgiszMonitorParser,
    staging_buffer: list[tuple[str | None, str, str, str | None]],
    fact_buffer: list[dict[str, Any]],
    stats: _PageStats,
    pipeline: str,
    progress_detail_cb: Callable[[EtlProgressPayload], None] | None,
    detail: Callable[[EtlProgressPayload], None],
    log: Callable[[str], None],
    licenses_modifydate_iso: str | None = None,
) -> None:
    """Парсинг и UPSERT одной порции строк журнала (LOGID уже выбраны из Firebird)."""

    def _d(payload: EtlProgressPayload) -> None:
        pl = dict(payload)
        pl["cursor_log_id"] = stats.last_id
        if licenses_modifydate_iso:
            pl["licenses_modifydate_iso"] = licenses_modifydate_iso
        detail(pl)  # type: ignore[arg-type]

    def on_stage(err: StagingParseError) -> None:
        staging_buffer.append((err.relates_to_id, err.error_code, err.message, err.log_excerpt))
        stats.staging_n += 1
        if pg is not None and len(staging_buffer) >= 200:
            insert_staging_errors(pg, staging_buffer)
            pg.commit()
            staging_buffer.clear()

    stats.fetched += len(rows)
    _d(
        {
            "phase": "exchangelog_parse",
            "loaded_rows": stats.fetched,
            "parsed_facts": stats.facts,
            "journal_facts": stats.facts,
            "staging_errors": stats.staging_n,
            "page": page,
            **_progress_payload_total_rows(total_exchangelog),
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

        joined_msg_egmid = _egmid_sql_int(r.get("message_egmid"))
        cached_msg_egmid = _egmid_sql_int(mrow.get("egmid")) if mrow else None
        row_msg_egmid = joined_msg_egmid if joined_msg_egmid is not None else cached_msg_egmid
        row_log_id = _fb_sql_bigint(r.get("logid"))

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
            exchangelog_log_id=row_log_id,
            egisz_messages_egmid=row_msg_egmid,
        )
        if progress_detail_cb and (row_i % 800 == 0 or row_i == len(rows)):
            _d(
                {
                    "phase": "parsing",
                    "loaded_rows": stats.fetched,
                    "parsed_facts": stats.facts,
                    "journal_facts": stats.facts,
                    "staging_errors": stats.staging_n,
                    "page": page,
                    **_progress_payload_total_rows(total_exchangelog),
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
        _upsert_facts_from_buffer(pg, fact_buffer, cfg)
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

    log(
        f"EXCHANGELOG: пакет {page} — строк в пакете {len(rows)}, всего обработано журнала {stats.fetched}, "
        f"LOGID курсор {stats.last_id}, фактов {stats.facts}."
    )
    _d(
        {
            "phase": "page_done",
            "loaded_rows": stats.fetched,
            "parsed_facts": stats.facts,
            "journal_facts": stats.facts,
            "staging_errors": stats.staging_n,
            "page": page,
            **_progress_payload_total_rows(total_exchangelog),
        }
    )


def _export_egisz_messages_by_egmid(
    cfg: CorpAppConfig,
    last_egmid: int,
    *,
    log: Callable[[str], None],
    detail: Callable[[EtlProgressPayload], None] | None = None,
) -> tuple[dict[str, dict[str, Any]], int]:
    """Выгрузка из Firebird: EGISZ_MESSAGES страницами с EGMID выше курсора (без окна по CREATEDATE)."""
    batch = max(1, cfg.etl.batch_size)
    msg_by_msgid: dict[str, dict[str, Any]] = {}
    cursor = int(last_egmid)
    total = 0
    page_n = 0
    last_batch_rows = 0
    if detail is not None:
        detail(
            {
                "phase": "messages_incremental",
                "loaded_rows": 0,
                "page": 0,
                "parsed_facts": 0,
                "journal_facts": 0,
                "staging_errors": 0,
                "messages_cursor_egmid": cursor,
                "messages_batch_rows": 0,
                "messages_msgid_cache_size": 0,
            }
        )
    log("Выгрузка EGISZ_MESSAGES из Firebird по курсору EGMID (без COUNT в Firebird)…")
    while True:
        page_n += 1
        prev_cursor = cursor
        sql = egisz_messages_incremental_sql(
            last_egmid=cursor,
            limit=batch,
        )
        rows = _etl_fb_fetch(cfg, sql)
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
        last_batch_rows = len(rows)
        if detail is not None and (
            page_n == 1 or page_n % 4 == 0 or len(rows) < batch
        ):
            detail(
                {
                    "phase": "messages_incremental",
                    "loaded_rows": total,
                    "page": page_n,
                    "parsed_facts": 0,
                    "journal_facts": 0,
                    "staging_errors": 0,
                    "messages_cursor_egmid": cursor,
                    "messages_batch_rows": last_batch_rows,
                    "messages_msgid_cache_size": len(msg_by_msgid),
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
    return _etl_fb_fetch(cfg, sql)


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

    page = 0
    lic_md_iso = (
        enrichment.max_licenses_modifydate.isoformat()
        if enrichment.max_licenses_modifydate
        else None
    )
    while True:
        page += 1
        rows = _export_exchangelog_page(
            cfg, base_sql, last_log_id=stats.last_id, limit=batch
        )
        if not rows:
            break
        _ingest_exchangelog_rows_chunk(
            cfg,
            pg,
            rows=rows,
            page=page,
            enrichment=enrichment,
            msg_by_msgid=msg_by_msgid,
            total_exchangelog=total_exchangelog,
            parser=parser,
            staging_buffer=staging_buffer,
            fact_buffer=fact_buffer,
            stats=stats,
            pipeline=pipeline,
            progress_detail_cb=progress_detail_cb,
            detail=detail,
            log=log,
            licenses_modifydate_iso=lic_md_iso,
        )
        if len(rows) < batch:
            break

    if pg is not None and fact_buffer:
        _upsert_facts_from_buffer(pg, fact_buffer, cfg)
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
    outbound_min_egmid: int,
    progress_detail_cb: Callable[[EtlProgressPayload], None] | None,
    log: Callable[[str], None],
    detail: Callable[[EtlProgressPayload], None],
) -> None:
    """Полная перезапись `stg_egisz_outbound_documents`: FB только DOCUMENTID/EGMID/даты/REPLYTO; KIND/JID — в Python."""
    log("Refreshing stg_egisz_outbound_documents (v_rpt_documents_no_response)...")

    pipeline = cfg.etl.pipeline_name
    lic_iso_out = (
        enrichment.max_licenses_modifydate.isoformat()
        if enrichment.max_licenses_modifydate
        else None
    )
    cursor_log_pg = get_last_log_id(pg, pipeline)
    metrics_ui: dict[str, Any] = {
        "cursor_log_id": cursor_log_pg,
        "licenses_modifydate_iso": lic_iso_out,
    }

    base_progress: dict[str, Any] = {
        "loaded_rows": progress_state["fetched"],
        "parsed_facts": progress_state["facts"],
        "journal_facts": progress_state["facts"],
        "staging_errors": progress_state["staging_n"],
    }
    base_progress.update(_progress_payload_total_rows(progress_state["total_exchangelog"]))
    detail({"phase": "outbound_firebird", **base_progress, **metrics_ui})  # type: ignore[arg-type]

    omsg = _etl_fb_fetch(
        cfg, outbound_documents_staging_select(min_egmid=outbound_min_egmid)
    )
    # SQL уже ORDER BY m.EGMID DESC: при монотонном EGMID первая строка на DOCUMENTID — самая новая.
    omsg_sorted = omsg
    outbound_n = len(omsg_sorted)
    detail(
        {
            "phase": "outbound_fetch",
            "outbound_total": outbound_n,
            "outbound_loaded": 0,
            **base_progress,
            **metrics_ui,
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
        if progress_detail_cb and (oi % 500 == 0 or oi == outbound_n):
            detail(
                {
                    "phase": "outbound_parse",
                    "outbound_total": outbound_n,
                    "outbound_loaded": oi,
                    "parsed_facts": len(stg_out),
                    **base_progress,
                    **metrics_ui,
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
            **metrics_ui,
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
            **metrics_ui,
        }  # type: ignore[arg-type]
    )


def _run_interleaved_messages_and_journal(
    cfg: CorpAppConfig,
    pg: Any,
    *,
    base_sql: str,
    enrichment: EnrichmentCache,
    last_egmid: int,
    last_log_id: int,
    total_exchangelog: int,
    progress_detail_cb: Callable[[EtlProgressPayload], None] | None,
    log: Callable[[str], None],
    detail: Callable[[EtlProgressPayload], None],
) -> tuple[_PageStats, int]:
    """Пакеты по 65k: EGISZ_MESSAGES (EGMID), затем EXCHANGELOG (LOGID); дозагрузка MSGID; фоновый SELECT сообщений пока парсится журнал."""
    chunk = _INTERLEAVE_PAGE_ROWS
    msg_by_msgid: dict[str, dict[str, Any]] = {}
    egmid_cursor = int(last_egmid)
    pipeline = cfg.etl.pipeline_name
    parser = EgiszMonitorParser()
    staging_buffer: list[tuple[str | None, str, str, str | None]] = []
    fact_buffer: list[dict[str, Any]] = []
    stats = _PageStats(last_id=last_log_id)
    lic_md_iso = (
        enrichment.max_licenses_modifydate.isoformat()
        if enrichment.max_licenses_modifydate
        else None
    )
    journal_page = 0
    msg_page = 0
    msg_total_loaded = 0
    last_msg_batch_rows = 0
    msg_done = False
    journal_done = False

    def msg_detail() -> None:
        detail(
            {
                "phase": "messages_incremental",
                "loaded_rows": msg_total_loaded,
                "page": msg_page,
                "parsed_facts": stats.facts,
                "journal_facts": stats.facts,
                "staging_errors": stats.staging_n,
                "messages_cursor_egmid": egmid_cursor,
                "messages_batch_rows": last_msg_batch_rows,
                "messages_msgid_cache_size": len(msg_by_msgid),
                "cursor_log_id": stats.last_id,
                "licenses_modifydate_iso": lic_md_iso,
            }
        )

    log(
        "Очередь EGISZ_MESSAGES → EXCHANGELOG: пакеты по 65000 строк из Firebird; COUNT не используется; "
        "пока парсится и пишется журнал, следующая страница EGISZ_MESSAGES читается параллельно."
    )

    with ThreadPoolExecutor(max_workers=1) as pool:
        fut_next_messages: Future | None = None

        while not (msg_done and journal_done):
            if not msg_done:
                rows_m: list[dict[str, Any]]
                new_c: int
                prev_c: int
                if fut_next_messages is not None:
                    rows_m, new_c, prev_c = fut_next_messages.result()
                    fut_next_messages = None
                else:
                    rows_m, new_c, prev_c = _messages_page_after_egmid(cfg, egmid_cursor, chunk)
                if not rows_m:
                    msg_done = True
                    log("EGISZ_MESSAGES: выше курсора EGMID строк нет (конец инкремента).")
                else:
                    _merge_messages_rows_into_map(rows_m, msg_by_msgid)
                    msg_total_loaded += len(rows_m)
                    egmid_cursor = new_c
                    msg_page += 1
                    last_msg_batch_rows = len(rows_m)
                    if msg_page == 1 or msg_page % 3 == 0 or len(rows_m) < chunk:
                        msg_detail()
                    if len(rows_m) >= chunk and new_c == prev_c:
                        log(
                            "Предупреждение: EGISZ_MESSAGES — полный пакет, курсор EGMID не сдвинулся; "
                            "останавливаем выгрузку сообщений (проверьте EGMID в Firebird и charset)."
                        )
                        msg_done = True
                    elif len(rows_m) < chunk:
                        msg_done = True

            if not journal_done:
                rows_j = _export_exchangelog_page(
                    cfg, base_sql, last_log_id=stats.last_id, limit=chunk
                )
                if not rows_j:
                    journal_done = True
                    log("EXCHANGELOG: новых строк по курсору LOGID нет.")
                else:
                    miss = _collect_missing_msgids_for_journal_rows(rows_j, msg_by_msgid)
                    if miss:
                        _backfill_messages_for_msgids(cfg, miss, msg_by_msgid, log)
                    if not msg_done:
                        fut_next_messages = pool.submit(
                            _messages_page_after_egmid, cfg, egmid_cursor, chunk
                        )
                    journal_page += 1
                    detail(
                        {
                            "phase": "exchangelog_export",
                            "loaded_rows": stats.fetched,
                            "parsed_facts": stats.facts,
                            "journal_facts": stats.facts,
                            "staging_errors": stats.staging_n,
                            "page": journal_page,
                            "cursor_log_id": stats.last_id,
                            "licenses_modifydate_iso": lic_md_iso,
                            **_progress_payload_total_rows(total_exchangelog),
                        }
                    )
                    _ingest_exchangelog_rows_chunk(
                        cfg,
                        pg,
                        rows=rows_j,
                        page=journal_page,
                        enrichment=enrichment,
                        msg_by_msgid=msg_by_msgid,
                        total_exchangelog=total_exchangelog,
                        parser=parser,
                        staging_buffer=staging_buffer,
                        fact_buffer=fact_buffer,
                        stats=stats,
                        pipeline=pipeline,
                        progress_detail_cb=progress_detail_cb,
                        detail=detail,
                        log=log,
                        licenses_modifydate_iso=lic_md_iso,
                    )
                    if len(rows_j) < chunk:
                        journal_done = True

    if pg is not None and fact_buffer:
        _upsert_facts_from_buffer(pg, fact_buffer, cfg)
        pg.commit()
    if pg is not None and staging_buffer:
        insert_staging_errors(pg, staging_buffer)
        pg.commit()

    log(
        f"Очередь сообщений+журнал завершена: EGISZ_MESSAGES накоплено {msg_total_loaded} строк, "
        f"уникальных MSGID {len(msg_by_msgid)}; EXCHANGELOG обработано строк {stats.fetched}, фактов {stats.facts}, LOGID={stats.last_id}."
    )
    return stats, egmid_cursor


def _read_cursor(cfg: CorpAppConfig, pg: Any, *, dry_run: bool) -> int:
    """Прочитать `etl_state.last_log_id` (для dry-run открыть отдельное соединение к PG)."""
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


def _read_egmid_cursor(cfg: CorpAppConfig, pg: Any, *, dry_run: bool) -> int:
    """Прочитать `etl_state.last_egmid` (курсор EGISZ_MESSAGES)."""
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
    """Оркестрация Firebird → Postgres: полная выгрузка лицензий (очистка в PostgreSQL); журнал EXCHANGELOG по LOGID;
    EGISZ_MESSAGES по EGMID; парсинг/UPSERT с сопоставлением MSGID в памяти."""
    cfg = cfg or load_corp_config()

    def log(msg: str) -> None:
        if progress_cb:
            progress_cb(msg)

    _detail_last_mono = 0.0
    _detail_last_phase: str | None = None

    def detail(payload: EtlProgressPayload) -> None:
        """Проброс прогресса в UI: смена фазы — сразу; та же фаза — не чаще чем раз в _DETAIL_THROTTLE_SEC."""
        if not progress_detail_cb:
            return
        nonlocal _detail_last_mono, _detail_last_phase
        ph = str((payload or {}).get("phase") or "")
        now = time.monotonic()
        if ph != _detail_last_phase:
            _detail_last_phase = ph
            _detail_last_mono = now
            progress_detail_cb(payload)
            return
        if (now - _detail_last_mono) < _DETAIL_THROTTLE_SEC:
            return
        _detail_last_mono = now
        progress_detail_cb(payload)

    base_sql = default_exchangelog_select()
    pipeline = cfg.etl.pipeline_name

    pg = None if dry_run else connect_pg(cfg.postgres)
    lock_acquired = False

    try:
        log(
            "ETL: четыре источника Firebird — JPERSONS и EGISZ_LICENSES (полная выгрузка в staging), "
            "EGISZ_MESSAGES по курсору EGMID и EXCHANGELOG по LOGID (инкремент); далее витрина и исходящие."
        )
        detail({"phase": "pipeline_bootstrap"})
        if pg is not None:
            log("PostgreSQL: применение DDL витрины (идемпотентно), ожидайте…")
            apply_reports_schema(pg)
            log("PostgreSQL: DDL применён; запрос advisory lock для пайплайна…")
            # Single-flight на уровне БД: блокирует параллельный запуск из CronJob и UI.
            # Lock освобождается при close() соединения, поэтому крэш не оставит «навечно занято».
            lock_acquired = try_acquire_pipeline_lock(pg, pipeline)
            if not lock_acquired:
                raise PipelineLockBusyError(
                    f"Sync пайплайна '{pipeline}' уже выполняется (advisory lock занят). "
                    "Дождитесь завершения текущего запуска или проверьте `pg_locks`."
                )
            log("PostgreSQL: lock получен; полная выгрузка JPERSONS и EGISZ_LICENSES из Firebird…")

        enrichment = _export_egisz_licenses_full(cfg, log, pg=pg)
        lic_iso = (
            enrichment.max_licenses_modifydate.isoformat()
            if enrichment.max_licenses_modifydate
            else None
        )
        detail(
            {
                "phase": "enrichment_firebird",
                "licenses_modifydate_iso": lic_iso,
            }
        )

        if pg is not None and not dry_run and enrichment.max_licenses_modifydate is not None:
            set_etl_source_peaks(pg, pipeline, None, enrichment.max_licenses_modifydate)
            pg.commit()

        last_id = _read_cursor(cfg, pg, dry_run=dry_run)
        last_egmid = _read_egmid_cursor(cfg, pg, dry_run=dry_run)
        outbound_egmid_floor = last_egmid
        log(f"ETL: last_log_id={last_id} last_egmid={last_egmid} pipeline={pipeline}")

        detail(
            {
                "phase": "counting",
                "cursor_log_id": last_id,
                "licenses_modifydate_iso": lic_iso,
            }
        )
        log("EXCHANGELOG: инкремент по LOGID; COUNT в Firebird для прогресса не выполняется.")
        total_exchangelog = _count_exchangelog_total()

        detail(
            {
                "phase": "exchangelog_ready",
                "loaded_rows": 0,
                "parsed_facts": 0,
                "journal_facts": 0,
                "staging_errors": 0,
                "cursor_log_id": last_id,
                "licenses_modifydate_iso": lic_iso,
                **_progress_payload_total_rows(total_exchangelog),
            }
        )

        page_stats, egmid_after_export = _run_interleaved_messages_and_journal(
            cfg,
            pg,
            base_sql=base_sql,
            enrichment=enrichment,
            last_egmid=last_egmid,
            last_log_id=last_id,
            total_exchangelog=total_exchangelog,
            progress_detail_cb=progress_detail_cb,
            log=log,
            detail=detail,
        )

        detail(
            {
                "phase": "exchangelog_done",
                "loaded_rows": page_stats.fetched,
                "parsed_facts": page_stats.facts,
                "journal_facts": page_stats.facts,
                "staging_errors": page_stats.staging_n,
                "cursor_log_id": page_stats.last_id,
                "licenses_modifydate_iso": lic_iso,
                **_progress_payload_total_rows(total_exchangelog),
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
                outbound_min_egmid=outbound_egmid_floor,
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
