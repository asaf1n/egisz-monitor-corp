"""Firebird → PostgreSQL ETL for corp fact table (LOGID cursor, not MODIFYDATE).

Термины (EGISZ_MESSAGES / журнал):
  • **MSGID** — идентификатор сообщения в контуре обмена и интеграций; в **EXCHANGELOG** ссылка в колонке **MSGID**;
    тело ответа РЭМД при наличии — в **EXCHANGELOG.MSGTEXT**.
  • **EGMID** — суррогатный ключ **строки** в таблице **EGISZ_MESSAGES** (фиксация исходящего при отправке в РЭМД ЕГИСЗ).

Источники Firebird в `run_sync`:
  • **JPERSONS** и **EGISZ_LICENSES** — первый шаг после lock: staging, merge **dim_clinics**, кэш для **build_record**.
  • **EGISZ_MESSAGES** и **EXCHANGELOG** — чередование страниц (``batch_size``): сообщения выгружаются в PostgreSQL
    `stg_egisz_messages_journal` **инкрементально по EGMID** с водяным знаком `etl_state.last_egmid`
    (и prune по окну дат при `sync_window_days > 0`); затем журнал EXCHANGELOG обрабатывается по LOGID.
    Сопоставление журнала с сообщениями выполняется уже в PostgreSQL по `MSGID`.
  • Исходящие: `_refresh_outbound_documents` (окно **CREATEDATE** при ``sync_window_days`` > 0).

Кооперативная остановка: проверка **перед** каждым SELECT к Firebird (справочники, страницы журнала и снимка сообщений).

Безопасность параллельного запуска: `pg_try_advisory_lock(hash(pipeline))`.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Mapping, Sequence, TypedDict

from egisz_monitor_corp.config_loader import CorpAppConfig, load_corp_config
from egisz_monitor_corp.fb_client import fetch_all
from egisz_monitor_corp.parser import EgiszMonitorParser, StagingParseError, extract_parse_hints
from egisz_monitor_corp.pg_warehouse import (
    PipelineLockBusyError,
    apply_reports_schema,
    connect_pg,
    ensure_etl_state_table,
    fetch_journal_messages_by_msgids,
    fetch_license_rows_for_enrichment,
    journal_msgids_present_in_staging,
    get_last_egmid,
    get_last_log_id,
    get_messages_snapshot_high_egmid,
    insert_journal_messages_staging_rows,
    insert_staging_errors,
    merge_dim_clinics_from_license_staging,
    refresh_license_staging_from_firebird_exports,
    refresh_outbound_documents_staging,
    release_pipeline_lock,
    set_etl_source_peaks,
    prune_stg_egisz_messages_journal_by_sync_window,
    set_last_egmid,
    set_last_log_id,
    set_messages_snapshot_high_egmid,
    try_acquire_pipeline_lock,
    truncate_journal_messages_staging,
    upsert_dim_clinic,
    upsert_dim_semd,
    upsert_facts_batch,
)
from egisz_monitor_corp.sql_util import (
    default_egisz_licenses_select_sql,
    default_jpersons_select_sql,
    egisz_messages_by_msgids_sql,
    exchangelog_inner_sql_for_etl,
    journal_messages_staging_base_sql,
    outbound_documents_staging_select,
    journal_messages_keyset_page_sql,
    paginated_exchangelog_sql,
)

CancelCheck = Callable[[], None] | None

# Минимальный интервал между одинаковыми фазами в progress_detail_cb (сек.): UI не должен замедлять ETL.
_DETAIL_THROTTLE_SEC = 0.22


def _etl_sync_window_days(cfg: CorpAppConfig) -> int | None:
    d = int(cfg.etl.sync_window_days)
    return d if d > 0 else None


def _messages_journal_full_rescan(cfg: CorpAppConfig) -> bool:
    """TRUNCATE снимка сообщений только в явном режиме полного пересъёма (sync_window_days < 0).

    Само окно для EXCHANGELOG/исходящих задаётся через `_etl_sync_window_days` → ``None`` — без предиката LOGDATE/CREATEDATE.
    """
    return int(cfg.etl.sync_window_days) < 0


def _full_sync_from_start(cfg: CorpAppConfig) -> bool:
    """Полная синхронизация "с нуля" для журнала и сообщений.

    При включении:
    - EXCHANGELOG идёт с LOGID > 0 (игнорируем last_log_id)
    - EGISZ_MESSAGES snapshot идёт с EGMID > 0 (игнорируем messages_snapshot_high_egmid)
    - окно по датам отключено (sync_window_days <= 0 => None в _etl_sync_window_days)
    """
    return int(cfg.etl.sync_window_days) < 0


def _etl_fb_fetch(
    cfg: CorpAppConfig,
    sql: str,
    params: Sequence[Any] | Mapping[str, Any] | None = None,
    *,
    wait_tick_sec: int | None = None,
    on_wait_tick: Callable[[int], None] | None = None,
) -> list[dict[str, Any]]:
    """SELECT к Firebird с таймаутом из конфига (COUNT и постраничные выгрузки)."""
    return fetch_all(
        cfg.firebird,
        sql,
        params,
        timeout_sec=cfg.etl.firebird_query_timeout_sec,
        wait_tick_sec=wait_tick_sec,
        on_wait_tick=on_wait_tick,
    )


def _max_license_modifydate(license_rows: list[dict[str, Any]]) -> Any:
    best: Any = None
    for r in license_rows:
        md = r.get("modifydate")
        if md is None:
            continue
        if best is None:
            best = md
            continue
        try:
            if md > best:
                best = md
        except TypeError:
            pass
    return best


def _best_license_row_by_jid(license_rows: list[dict[str, Any]]) -> dict[int, dict[str, Any]]:
    """Первая строка на JID в порядке fetch (ORDER BY jid, modifydate DESC …) — самая свежая лицензия."""
    out: dict[int, dict[str, Any]] = {}
    for r in license_rows:
        ji = _to_int(r.get("jid"))
        if ji is None:
            continue
        if ji not in out:
            out[ji] = r
    return out


def _build_mo_uid_to_jid(license_rows: list[dict[str, Any]]) -> dict[str, int]:
    """MO_UID → JID; при нескольких строках на один MO_UID — по наиболее свежему MODIFYDATE."""

    def _md_utc(r: dict[str, Any]) -> datetime:
        md = r.get("modifydate")
        if isinstance(md, datetime):
            if md.tzinfo is None:
                return md.replace(tzinfo=timezone.utc)
            return md.astimezone(timezone.utc)
        return datetime.min.replace(tzinfo=timezone.utc)

    sorted_rows = sorted(license_rows, key=_md_utc, reverse=True)
    out: dict[str, int] = {}
    for r in sorted_rows:
        raw = r.get("mo_uid")
        mou = (str(raw).strip() if raw is not None else "") or ""
        if not mou:
            continue
        ji = _to_int(r.get("jid"))
        if ji is not None and mou not in out:
            out[mou] = ji
    return out


def _license_row_for_host_jids(
    host_part: dict[str, Any], best_by_jid: dict[int, dict[str, Any]]
) -> dict[str, Any] | None:
    for key in ("jid_url", "jid_gost_log", "jid_gost_reply"):
        j = host_part.get(key)
        if isinstance(j, int) and j > 0:
            row = best_by_jid.get(j)
            if row:
                return row
    return None


def _load_reference_tables(
    cfg: CorpAppConfig,
    pg: Any,
    *,
    pipeline: str,
    log: Callable[[str], None],
    detail: Callable[[EtlProgressPayload], None],
    cancel_check: CancelCheck,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """JPERSONS затем EGISZ_LICENSES из Firebird; staging + dim_clinics; пик MODIFYDATE лицензий в etl_state."""
    _raise_if_cancel(cancel_check)
    jp_rows = _etl_fb_fetch(cfg, default_jpersons_select_sql())
    _raise_if_cancel(cancel_check)
    detail(
        {
            "phase": "references_jpersons_export",
            "loaded_rows": len(jp_rows),
            "parsed_facts": 0,
            "journal_facts": 0,
            "staging_errors": 0,
        }
    )
    log(f"JPERSONS: выгрузка из Firebird — {len(jp_rows)} строк.")
    lic_rows = _etl_fb_fetch(cfg, default_egisz_licenses_select_sql())
    _raise_if_cancel(cancel_check)
    detail(
        {
            "phase": "references_licenses_export",
            "loaded_rows": len(lic_rows),
            "parsed_facts": 0,
            "journal_facts": 0,
            "staging_errors": 0,
        }
    )
    log(f"EGISZ_LICENSES: выгрузка из Firebird — {len(lic_rows)} строк.")
    _raise_if_cancel(cancel_check)
    detail(
        {
            "phase": "references_pg_staging",
            "loaded_rows": 0,
            "parsed_facts": 0,
            "journal_facts": 0,
            "staging_errors": 0,
        }
    )
    ichunk = max(500, min(int(cfg.etl.batch_size), 20_000))
    refresh_license_staging_from_firebird_exports(
        pg,
        jpersons_rows=jp_rows,
        license_rows=lic_rows,
        insert_chunk_size=ichunk,
    )
    detail(
        {
            "phase": "references_merge_dim",
            "loaded_rows": 0,
            "parsed_facts": 0,
            "journal_facts": 0,
            "staging_errors": 0,
        }
    )
    merge_dim_clinics_from_license_staging(pg)
    pg.commit()
    peak_lic = _max_license_modifydate(lic_rows)
    set_etl_source_peaks(pg, pipeline, None, peak_lic)
    pg.commit()
    log(
        "Справочники: запись staging, merge dim_clinics (JPERSONS + EGISZ_LICENSES), пик лицензий в etl_state."
    )
    return jp_rows, lic_rows


def _ensure_exchangelog_msgids_in_staging_from_firebird(
    cfg: CorpAppConfig,
    pg: Any,
    journal_rows: list[dict[str, Any]],
    *,
    log: Callable[[str], None],
    cancel_check: CancelCheck = None,
) -> None:
    """Строки EGISZ_MESSAGES по MSGID из пакета журнала, отсутствующие в staging, — SELECT из Firebird и UPSERT в PG."""
    ids: list[str] = []
    seen: set[str] = set()
    for r in journal_rows:
        mk = _norm_msgid_key(r.get("msgid"))
        if mk and mk not in seen:
            seen.add(mk)
            ids.append(mk)
    if not ids or pg is None:
        return
    present = journal_msgids_present_in_staging(pg, ids)
    missing = [m for m in ids if m not in present]
    if not missing:
        return
    qmax = max(1, min(int(cfg.etl.batch_size), 500))
    loaded = 0
    for i in range(0, len(missing), qmax):
        _raise_if_cancel(cancel_check)
        part = missing[i : i + qmax]
        ph = ", ".join(["?"] * len(part))
        sql = egisz_messages_by_msgids_sql(ph)
        fb_rows = _etl_fb_fetch(cfg, sql, tuple(part))
        if fb_rows:
            insert_journal_messages_staging_rows(pg, fb_rows)
            pg.commit()
            loaded += len(fb_rows)
    if loaded:
        log(
            f"EGISZ_MESSAGES: догрузка по MSGID из пакета журнала — {loaded} строк из Firebird "
            f"(уникальных MSGID не в staging: {len(missing)})."
        )


def _max_egmid_in_fb_message_rows(rows: list[dict[str, Any]]) -> int | None:
    best: int | None = None
    for r in rows:
        v = _egmid_sql_int(r.get("egmid"))
        if v is None:
            continue
        vi = int(v)
        if best is None or vi > best:
            best = vi
    return best


def _journal_messages_staging_fetch_keyset_page(
    cfg: CorpAppConfig,
    pg: Any,
    *,
    base_inner: str,
    after_egmid: int,
    batch: int,
    page: int,
    total_before: int,
    detail: Callable[[EtlProgressPayload], None],
    cancel_check: CancelCheck,
) -> tuple[int, int, bool]:
    """Одна страница снимка EGISZ_MESSAGES (EGMID > after) → staging. Возвращает (новый after_egmid, прирост строк, конец)."""
    _raise_if_cancel(cancel_check)
    base_pl: dict[str, Any] = {
        "loaded_rows": total_before,
        "parsed_facts": 0,
        "journal_facts": 0,
        "staging_errors": 0,
        "page": page,
        "journal_batch_rows": batch,
    }
    detail({"phase": "journal_messages_export_firebird", **base_pl})  # type: ignore[arg-type]
    sql = journal_messages_keyset_page_sql(base_inner, after_egmid=after_egmid, limit=batch)
    fb_timeout = max(1, int(cfg.etl.firebird_query_timeout_sec))
    tick_sec = min(15, max(5, fb_timeout // 60))

    def _fb_wait_tick(elapsed_sec: int) -> None:
        _raise_if_cancel(cancel_check)
        detail(  # type: ignore[arg-type]
            {
                "phase": "journal_messages_export_firebird",
                **base_pl,
                "firebird_elapsed_sec": int(elapsed_sec),
            }
        )

    rows = _etl_fb_fetch(
        cfg,
        sql,
        wait_tick_sec=tick_sec,
        on_wait_tick=_fb_wait_tick,
    )
    if not rows:
        return after_egmid, 0, True
    detail({"phase": "journal_messages_export_postgres", **base_pl})  # type: ignore[arg-type]
    insert_journal_messages_staging_rows(pg, rows)
    pg.commit()
    mx = _max_egmid_in_fb_message_rows(rows)
    new_after = int(mx) if mx is not None else after_egmid
    n = len(rows)
    detail(
        {
            "phase": "journal_messages_export_done",
            "loaded_rows": total_before + n,
            "parsed_facts": 0,
            "journal_facts": 0,
            "staging_errors": 0,
            "page": page,
            "journal_batch_rows": n,
        }  # type: ignore[arg-type]
    )
    return new_after, n, n < batch


def _sync_journal_snapshot_interleaved(
    cfg: CorpAppConfig,
    pg: Any,
    *,
    base_sql: str,
    last_id: int,
    total_exchangelog: int,
    progress_detail_cb: Callable[[EtlProgressPayload], None] | None,
    log: Callable[[str], None],
    detail: Callable[[EtlProgressPayload], None],
    cancel_check: CancelCheck = None,
    best_lic_by_jid: dict[int, dict[str, Any]] | None = None,
    mo_uid_to_jid: dict[str, int] | None = None,
) -> _PageStats:
    """Чередование страниц снимка EGISZ_MESSAGES (EGMID keyset) и журнала EXCHANGELOG (LOGID cursor)."""
    if pg is None:
        return _process_exchangelog_pages(
            cfg,
            pg,
            base_sql=base_sql,
            last_id=last_id,
            total_exchangelog=total_exchangelog,
            progress_detail_cb=progress_detail_cb,
            log=log,
            detail=detail,
            cancel_check=cancel_check,
            best_lic_by_jid=best_lic_by_jid,
            mo_uid_to_jid=mo_uid_to_jid,
        )

    batch = max(1, cfg.etl.batch_size)
    base_msgs = journal_messages_staging_base_sql(sync_window_days=_etl_sync_window_days(cfg))
    pipeline = cfg.etl.pipeline_name

    if _messages_journal_full_rescan(cfg):
        truncate_journal_messages_staging(pg)
        set_messages_snapshot_high_egmid(pg, pipeline, 0)
        pg.commit()
        msg_scan = 0
    else:
        prune_stg_egisz_messages_journal_by_sync_window(pg, _etl_sync_window_days(cfg))
        msg_scan = get_messages_snapshot_high_egmid(pg, pipeline)
        pg.commit()

    parser = EgiszMonitorParser()
    staging_buffer: list[tuple] = []
    fact_buffer: list[dict[str, Any]] = []
    stats = _PageStats(last_id=last_id, messages_snapshot_scan_high_egmid=msg_scan)

    msg_page = 0
    msg_total = 0
    msgs_done = False
    journal_exhausted = False
    journal_page = 0

    while not journal_exhausted:
        _raise_if_cancel(cancel_check)
        # Важно для UX: EXCHANGELOG и EGISZ_MESSAGES должны "чередоваться" в логе.
        # Если Firebird-запрос к EGISZ_MESSAGES долгий, оператор всё равно должен видеть,
        # что журнал EXCHANGELOG тоже обрабатывается (и наоборот). Поэтому пакет журнала — первым.
        journal_page += 1
        detail(
            {
                "phase": "exchangelog_export",
                "loaded_rows": stats.fetched,
                "parsed_facts": stats.facts,
                "journal_facts": stats.facts,
                "staging_errors": stats.staging_n,
                "page": journal_page,
                "journal_batch_rows": batch,
                "cursor_log_id": stats.last_id,
                **_progress_payload_total_rows(total_exchangelog),
            }
        )
        jrows = _export_exchangelog_page(cfg, base_sql, last_log_id=stats.last_id, limit=batch)
        if not jrows:
            journal_exhausted = True
            break
        log(f"EXCHANGELOG: пакет {journal_page} — выгрузка из Firebird, строк {len(jrows)}.")
        _ensure_exchangelog_msgids_in_staging_from_firebird(
            cfg, pg, jrows, log=log, cancel_check=cancel_check
        )
        _ingest_exchangelog_rows_chunk(
            cfg,
            pg,
            rows=jrows,
            page=journal_page,
            total_exchangelog=total_exchangelog,
            parser=parser,
            staging_buffer=staging_buffer,
            fact_buffer=fact_buffer,
            stats=stats,
            pipeline=pipeline,
            progress_detail_cb=progress_detail_cb,
            detail=detail,
            log=log,
            cancel_check=cancel_check,
            best_lic_by_jid=best_lic_by_jid,
            mo_uid_to_jid=mo_uid_to_jid,
        )
        if len(jrows) < batch:
            journal_exhausted = True

        if not msgs_done:
            msg_page += 1
            msg_scan, inc, partial = _journal_messages_staging_fetch_keyset_page(
                cfg,
                pg,
                base_inner=base_msgs,
                after_egmid=msg_scan,
                batch=batch,
                page=msg_page,
                total_before=msg_total,
                detail=detail,
                cancel_check=cancel_check,
            )
            msg_total += inc
            stats.messages_snapshot_scan_high_egmid = msg_scan
            if partial:
                msgs_done = True

    while not msgs_done:
        _raise_if_cancel(cancel_check)
        msg_page += 1
        msg_scan, inc, partial = _journal_messages_staging_fetch_keyset_page(
            cfg,
            pg,
            base_inner=base_msgs,
            after_egmid=msg_scan,
            batch=batch,
            page=msg_page,
            total_before=msg_total,
            detail=detail,
            cancel_check=cancel_check,
        )
        msg_total += inc
        stats.messages_snapshot_scan_high_egmid = msg_scan
        if partial:
            msgs_done = True

    log(
        f"EGISZ_MESSAGES: выгрузка из Firebird в PostgreSQL — {msg_total} строк в stg_egisz_messages_journal "
        f"(инкремент по EGMID, high={stats.messages_snapshot_scan_high_egmid})."
    )

    if fact_buffer:
        _upsert_facts_from_buffer(pg, fact_buffer, cfg)
        pg.commit()
    if staging_buffer:
        insert_staging_errors(pg, staging_buffer)
        pg.commit()

    return stats


@dataclass
class EtlRunStats:
    fetched: int
    facts_upserted: int
    staging_errors: int
    max_log_id: int
    last_cursor_after: int


class EtlCancelledError(Exception):
    """Остановка синхронизации по запросу оператора (Config UI)."""


def _raise_if_cancel(cancel_check: CancelCheck) -> None:
    if cancel_check is not None:
        cancel_check()


class EtlProgressPayload(TypedDict, total=False):
    """Снимок прогресса для UI (все поля опциональны кроме phase).

    phase: pipeline_bootstrap | pg_schema_apply | references_* |
           counting | exchangelog_* | outbound_* | sync_failed …
    """

    phase: str
    pipeline_step: int
    pipeline_steps: int
    total_rows: int
    loaded_rows: int
    parsed_facts: int
    staging_errors: int
    page: int
    outbound_loaded: int
    outbound_total: int
    journal_facts: int
    etl_last_egmid: int
    messages_batch_rows: int
    messages_msgid_cache_size: int
    journal_batch_rows: int
    cursor_log_id: int
    firebird_elapsed_sec: int
    # "Документы" — не по строкам, а по уникальным идентификаторам (localUid/emdrId).
    documents_unique: int
    documents_localuid_unique: int
    documents_emdrid_unique: int
    outbound_total_docs: int
    outbound_loaded_docs: int


def _to_int(v: Any) -> int | None:
    if v is None:
        return None
    try:
        n = int(v)
        return n if n > 0 else None
    except (TypeError, ValueError):
        return None


def _egmid_sql_int(v: Any) -> int | None:
    """EGMID как целое (0 допустим для last_egmid из etl_state; иначе _to_int отбрасывает 0)."""
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


def _is_test_clinic(jname: str | None) -> bool:
    """Тестовые клиники: jname содержит 'test' или 'тест' (в нижнем регистре). Решение зафиксировано в .cursorrules."""
    if not jname:
        return False
    low = jname.lower()
    return "test" in low or "тест" in low


def _build_msg_cache_for_journal_page(
    cfg: CorpAppConfig,
    pg: Any,
    rows: list[dict[str, Any]],
    log: Callable[[str], None],
    *,
    cancel_check: CancelCheck = None,
) -> dict[str, dict[str, Any]]:
    """MSGID → поля сообщения: из PostgreSQL stg_egisz_messages_journal (после снимка EGISZ_MESSAGES с Firebird)."""
    cache: dict[str, dict[str, Any]] = {}
    ids: list[str] = []
    seen: set[str] = set()
    for r in rows:
        mk = _norm_msgid_key(r.get("msgid"))
        if mk and mk not in seen:
            seen.add(mk)
            ids.append(mk)
    if not ids or pg is None:
        return cache
    qbatch = max(1, min(int(cfg.etl.batch_size), 10_000))
    n_pg = 0
    for i in range(0, len(ids), qbatch):
        _raise_if_cancel(cancel_check)
        part = ids[i : i + qbatch]
        got = fetch_journal_messages_by_msgids(pg, part)
        n_pg += len(got)
        for row in got:
            mk = _norm_msgid_key(row.get("msgid"))
            if not mk:
                continue
            cache[mk] = {
                "msgid": mk,
                "replyto": row.get("replyto"),
                "documentid": row.get("documentid"),
                "egmid": row.get("egmid"),
                "msg_created_at": row.get("msg_created_at"),
            }
    if n_pg:
        log(
            f"EGISZ_MESSAGES: сопоставление из PostgreSQL — {n_pg} строк по MSGID в пакете журнала, "
            f"уникальных MSGID в кэше {len(cache)}."
        )
    return cache


def _upsert_facts_from_buffer(pg: Any, fact_buffer: list[dict[str, Any]], cfg: CorpAppConfig) -> None:
    upsert_facts_batch(
        pg,
        fact_buffer,
        chunk_size=cfg.etl.facts_upsert_chunk_size,
        commit_each_chunk=True,
        statement_timeout_sec=cfg.etl.pg_upsert_statement_timeout_sec,
    )


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
    max_egmid_seen: int = 0
    messages_snapshot_scan_high_egmid: int = 0
    # Уникальные "документы" по идентификаторам (не по строкам журнала).
    _doc_keys: set[str] = field(default_factory=set, repr=False)
    _doc_localuid: set[str] = field(default_factory=set, repr=False)
    _doc_emdrid: set[str] = field(default_factory=set, repr=False)

    def note_document_ids(self, *, local_uid: str | None, emdr_id: str | None) -> None:
        lu = (local_uid or "").strip()
        ei = (emdr_id or "").strip()
        if lu:
            self._doc_keys.add(f"lu:{lu}")
            self._doc_localuid.add(lu)
        if ei:
            self._doc_keys.add(f"emdr:{ei}")
            self._doc_emdrid.add(ei)

    def documents_unique(self) -> int:
        return len(self._doc_keys)

    def documents_localuid_unique(self) -> int:
        return len(self._doc_localuid)

    def documents_emdrid_unique(self) -> int:
        return len(self._doc_emdrid)


def _ingest_exchangelog_rows_chunk(
    cfg: CorpAppConfig,
    pg: Any,
    *,
    rows: list[dict[str, Any]],
    page: int,
    total_exchangelog: int,
    parser: EgiszMonitorParser,
    staging_buffer: list[tuple],
    fact_buffer: list[dict[str, Any]],
    stats: _PageStats,
    pipeline: str,
    progress_detail_cb: Callable[[EtlProgressPayload], None] | None,
    detail: Callable[[EtlProgressPayload], None],
    log: Callable[[str], None],
    cancel_check: CancelCheck = None,
    best_lic_by_jid: dict[int, dict[str, Any]] | None = None,
    mo_uid_to_jid: dict[str, int] | None = None,
) -> None:
    """Парсинг и UPSERT одной порции строк журнала; поля EGISZ_MESSAGES — из PostgreSQL по MSGID."""

    def _d(payload: EtlProgressPayload) -> None:
        pl = dict(payload)
        pl["cursor_log_id"] = stats.last_id
        pl["documents_unique"] = stats.documents_unique()
        pl["documents_localuid_unique"] = stats.documents_localuid_unique()
        pl["documents_emdrid_unique"] = stats.documents_emdrid_unique()
        detail(pl)  # type: ignore[arg-type]

    def on_stage(err: StagingParseError) -> None:
        staging_buffer.append(err.as_insert_tuple())
        stats.staging_n += 1
        if pg is not None and len(staging_buffer) >= 200:
            insert_staging_errors(pg, staging_buffer)
            pg.commit()
            staging_buffer.clear()

    msg_by_msgid = _build_msg_cache_for_journal_page(cfg, pg, rows, log, cancel_check=cancel_check)

    best_map = best_lic_by_jid or {}
    mo_map = mo_uid_to_jid or {}

    stats.fetched += len(rows)
    _d(
        {
            "phase": "exchangelog_parse",
            "loaded_rows": stats.fetched,
            "parsed_facts": stats.facts,
            "journal_facts": stats.facts,
            "staging_errors": stats.staging_n,
            "page": page,
            "journal_batch_rows": len(rows),
            "messages_msgid_cache_size": len(msg_by_msgid),
            **_progress_payload_total_rows(total_exchangelog),
        }
    )

    for row_i, r in enumerate(rows, start=1):
        lid = _fb_sql_bigint(r.get("logid"))
        if lid is not None and lid > stats.max_log_id:
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

        # EGMID только из снимка stg_egisz_messages_journal (журнал EXCHANGELOG в Firebird без JOIN к EGISZ_MESSAGES).
        row_msg_egmid = _egmid_sql_int(mrow.get("egmid")) if mrow else None
        if row_msg_egmid is not None and row_msg_egmid > stats.max_egmid_seen:
            stats.max_egmid_seen = int(row_msg_egmid)
        row_log_id = _fb_sql_bigint(r.get("logid"))

        host_part = parser.extract_jid(logtext, reply_to=reply_to, msg_text=msgtext)
        lic_r = _license_row_for_host_jids(host_part, best_map)
        kind_from = lic_r.get("egisz_licenses_kind") if lic_r else None
        mo_uid_from = _to_str(lic_r.get("mo_uid")) if lic_r else None
        jid_from_row = _to_int(lic_r.get("jid")) if lic_r else None

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
                rth, luh, emh = extract_parse_hints(msgtext)
                on_stage(
                    StagingParseError(
                        relates_to_id=None,
                        error_code="MSGTEXT_TOO_LARGE",
                        message=f"MSGTEXT UTF-8 size {nbytes} exceeds max_msgtext_bytes={lim}",
                        log_excerpt=excerpt,
                        exchangelog_log_id=row_log_id,
                        egisz_messages_egmid=row_msg_egmid,
                        journal_msgid=mk,
                        relates_to_hint=rth,
                        local_uid_hint=luh,
                        emdr_id_hint=emh,
                    )
                )
                continue

        rec = parser.build_record(
            logtext,
            msg_text=msgtext,
            kind_from_egisz_licenses=kind_from,
            mo_uid_from_egisz_licenses=mo_uid_from,
            jid_from_egisz_licenses_row=jid_from_row,
            jid_by_mo_uid_from_egisz_licenses=mo_map,
            reply_to=reply_to,
            document_id=doc_id,
            msg_created_at=msg_created,
            log_created_at=log_created,
            on_staging_error=on_stage,
            exchangelog_log_id=row_log_id,
            egisz_messages_egmid=row_msg_egmid,
            journal_msgid=mk,
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
                    "journal_batch_rows": len(rows),
                    **_progress_payload_total_rows(total_exchangelog),
                }
            )

        if rec is None:
            continue

        stats.note_document_ids(local_uid=rec.local_uid_semd, emdr_id=rec.emdr_id)
        fact_buffer.append(rec.as_fact_row())
        stats.facts += 1

        if rec.kind_code and rec.kind_name and pg is not None:
            upsert_dim_semd(pg, rec.kind_code, rec.kind_name)

        if rec.jid and rec.jid > 0 and pg is not None:
            upsert_dim_clinic(pg, rec.jid, None, rec.org_oid, jinn=None, fir_oid=None)

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
        f"EXCHANGELOG: пакет {page} — парсинг и UPSERT: всего журнала {stats.fetched}, "
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
            "journal_batch_rows": len(rows),
            **_progress_payload_total_rows(total_exchangelog),
        }
    )


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
    last_id: int,
    total_exchangelog: int,
    progress_detail_cb: Callable[[EtlProgressPayload], None] | None,
    log: Callable[[str], None],
    detail: Callable[[EtlProgressPayload], None],
    cancel_check: CancelCheck = None,
    best_lic_by_jid: dict[int, dict[str, Any]] | None = None,
    mo_uid_to_jid: dict[str, int] | None = None,
) -> _PageStats:
    """Постранично: EXCHANGELOG из Firebird; EGISZ_MESSAGES — догрузка по MSGID из пакетов; парсинг, UPSERT."""
    pipeline = cfg.etl.pipeline_name
    batch = max(1, cfg.etl.batch_size)
    parser = EgiszMonitorParser()
    staging_buffer: list[tuple] = []
    fact_buffer: list[dict[str, Any]] = []
    stats = _PageStats(last_id=last_id)

    page = 0
    while True:
        _raise_if_cancel(cancel_check)
        page += 1
        detail(
            {
                "phase": "exchangelog_export",
                "loaded_rows": stats.fetched,
                "parsed_facts": stats.facts,
                "journal_facts": stats.facts,
                "staging_errors": stats.staging_n,
                "page": page,
                "journal_batch_rows": batch,
                "cursor_log_id": stats.last_id,
                **_progress_payload_total_rows(total_exchangelog),
            }
        )
        rows = _export_exchangelog_page(
            cfg, base_sql, last_log_id=stats.last_id, limit=batch
        )
        if not rows:
            break
        log(
            f"EXCHANGELOG: пакет {page} — выгрузка из Firebird, строк {len(rows)}."
        )
        _ingest_exchangelog_rows_chunk(
            cfg,
            pg,
            rows=rows,
            page=page,
            total_exchangelog=total_exchangelog,
            parser=parser,
            staging_buffer=staging_buffer,
            fact_buffer=fact_buffer,
            stats=stats,
            pipeline=pipeline,
            progress_detail_cb=progress_detail_cb,
            detail=detail,
            log=log,
            cancel_check=cancel_check,
            best_lic_by_jid=best_lic_by_jid,
            mo_uid_to_jid=mo_uid_to_jid,
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
    progress_state: dict[str, int],
    progress_detail_cb: Callable[[EtlProgressPayload], None] | None,
    log: Callable[[str], None],
    detail: Callable[[EtlProgressPayload], None],
    cancel_check: CancelCheck = None,
) -> None:
    """Полная перезапись `stg_egisz_outbound_documents`: Firebird EGISZ_MESSAGES в окне CREATEDATE (как журнал)."""
    log("Refreshing stg_egisz_outbound_documents (v_rpt_documents_no_response)...")

    pipeline = cfg.etl.pipeline_name
    cursor_log_pg = get_last_log_id(pg, pipeline)
    metrics_ui: dict[str, Any] = {"cursor_log_id": cursor_log_pg}

    base_progress: dict[str, Any] = {
        "loaded_rows": progress_state["fetched"],
        "parsed_facts": progress_state["facts"],
        "journal_facts": progress_state["facts"],
        "staging_errors": progress_state["staging_n"],
    }
    base_progress.update(_progress_payload_total_rows(progress_state["total_exchangelog"]))
    detail({"phase": "outbound_firebird", **base_progress, **metrics_ui})  # type: ignore[arg-type]

    _raise_if_cancel(cancel_check)
    omsg = _etl_fb_fetch(
        cfg,
        outbound_documents_staging_select(sync_window_days=_etl_sync_window_days(cfg)),
    )
    omsg_sorted = omsg
    outbound_n = len(omsg_sorted)
    # Для UI считаем "документы" как уникальные DOCUMENTID (localUid), а не как строки выборки.
    outbound_total_docs = len(
        {
            _to_str(r.get("documentid"))
            for r in omsg_sorted
            if _to_str(r.get("documentid"))
        }
    )
    detail(
        {
            "phase": "outbound_fetch",
            "outbound_total": outbound_n,
            "outbound_total_docs": outbound_total_docs,
            "outbound_loaded": 0,
            "outbound_loaded_docs": 0,
            **base_progress,
            **metrics_ui,
        }  # type: ignore[arg-type]
    )

    parser_oob = EgiszMonitorParser()
    stg_out: list[dict[str, Any]] = []
    seen_doc: set[str] = set()
    for oi, r in enumerate(omsg_sorted, start=1):
        if oi == 1 or oi % 400 == 0:
            _raise_if_cancel(cancel_check)
        did = _to_str(r.get("documentid"))
        skip = not did or did in seen_doc
        reply_to = _to_str(r.get("replyto"))
        if not skip:
            seen_doc.add(did)  # type: ignore[arg-type]
            host_part = parser_oob.extract_jid(None, reply_to=reply_to)
            stg_out.append(
                {
                    "document_id": did,
                    "sent_at": _sent_at_utc(r.get("msg_sent_at")),
                    "reply_to": reply_to,
                    "gost_jid_token": host_part.get("gost_jid_token"),
                    "kind_code": None,
                    "jid": None,
                    "egmid": _egmid_sql_int(r.get("egmid")),
                }
            )
        loaded_docs = len(seen_doc)
        if progress_detail_cb and (oi % 500 == 0 or oi == outbound_n):
            detail(
                {
                    "phase": "outbound_parse",
                    "outbound_total": outbound_n,
                    "outbound_total_docs": outbound_total_docs,
                    "outbound_loaded": oi,
                    "outbound_loaded_docs": loaded_docs,
                    "parsed_facts": len(stg_out),
                    **base_progress,
                    **metrics_ui,
                }  # type: ignore[arg-type]
            )

    og_total = len(stg_out)
    detail(
        {
            "phase": "outbound_postgres",
            "outbound_total": outbound_n,
            "outbound_total_docs": og_total,
            "outbound_loaded": 0,
            "outbound_loaded_docs": 0,
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
            "outbound_total": outbound_n,
            "outbound_total_docs": og_total,
            "outbound_loaded": outbound_n,
            "outbound_loaded_docs": og_total,
            "parsed_facts": og_total,
            **base_progress,
            **metrics_ui,
        }  # type: ignore[arg-type]
    )


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
    """Прочитать `etl_state.last_egmid` — ватермарк по max(EGMID) из обработанного журнала (EGMID = ключ строки EGISZ_MESSAGES)."""
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
    cancel_check: CancelCheck = None,
) -> EtlRunStats:
    """Оркестрация Firebird → Postgres: справочники первыми; чередование снимка EGISZ_MESSAGES и EXCHANGELOG; исходящие."""
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

    base_sql = exchangelog_inner_sql_for_etl(sync_window_days=_etl_sync_window_days(cfg))
    pipeline = cfg.etl.pipeline_name

    pg = None if dry_run else connect_pg(cfg.postgres)
    lock_acquired = False
    bootstrap_total = 3 if pg is not None else 1

    def boot_detail(n: int, phase: str, **extra: Any) -> None:
        pl: dict[str, Any] = {"phase": phase}
        if n >= 1:
            pl["pipeline_step"] = n
            pl["pipeline_steps"] = bootstrap_total
        pl.update(extra)
        detail(pl)  # type: ignore[arg-type]

    try:
        log(
            "ETL: первый шаг данных — JPERSONS и EGISZ_LICENSES → PostgreSQL (staging, merge dim_clinics)."
        )
        log(
            "ETL: EGISZ_MESSAGES и EXCHANGELOG — чередование страниц с ранних записей; "
            "недостающие MSGID для пакета журнала подгружаются из Firebird; затем исходящие в staging."
        )
        boot_detail(0, "pipeline_bootstrap")
        if pg is not None:
            log("PostgreSQL: применение DDL витрины (идемпотентно), ожидайте…")
            boot_detail(1, "pg_schema_apply")
            apply_reports_schema(pg)
            log("PostgreSQL: DDL применён; запрос advisory lock для пайплайна…")
            boot_detail(2, "pg_advisory_lock")
            lock_acquired = try_acquire_pipeline_lock(pg, pipeline)
            if not lock_acquired:
                raise PipelineLockBusyError(
                    f"Sync пайплайна '{pipeline}' уже выполняется (advisory lock занят). "
                    "Дождитесь завершения текущего запуска или проверьте `pg_locks`."
                )
            log("PostgreSQL: lock получен.")

        _raise_if_cancel(cancel_check)

        reference_max_license_modifydate: Any = None
        license_sorted: list[dict[str, Any]] = []
        mo_uid_to_jid: dict[str, int] = {}
        best_lic_by_jid: dict[int, dict[str, Any]] = {}
        if pg is not None and not dry_run:
            _, lic_fb_rows = _load_reference_tables(
                cfg,
                pg,
                pipeline=pipeline,
                log=log,
                detail=detail,
                cancel_check=cancel_check,
            )
            reference_max_license_modifydate = _max_license_modifydate(lic_fb_rows)
            license_sorted = fetch_license_rows_for_enrichment(pg)
            mo_uid_to_jid = _build_mo_uid_to_jid(license_sorted)
            best_lic_by_jid = _best_license_row_by_jid(license_sorted)

        last_id = _read_cursor(cfg, pg, dry_run=dry_run)
        last_egmid = _read_egmid_cursor(cfg, pg, dry_run=dry_run)

        # Полная синхронизация: пройти EXCHANGELOG и EGISZ_MESSAGES "с начала", игнорируя курсоры.
        # Это делается только в явном режиме (sync_window_days < 0).
        if pg is not None and not dry_run and _full_sync_from_start(cfg):
            log("Полная синхронизация: сброс курсоров etl_state и полный проход EXCHANGELOG/EGISZ_MESSAGES с начала.")
            boot_detail(0, "pipeline_bootstrap")
            set_last_log_id(pg, pipeline, 0)
            set_last_egmid(pg, pipeline, 0)
            set_messages_snapshot_high_egmid(pg, pipeline, 0)
            truncate_journal_messages_staging(pg)
            pg.commit()
            last_id = 0
            last_egmid = 0
        log(f"ETL: last_log_id={last_id} last_egmid={last_egmid} pipeline={pipeline}")

        detail(
            {
                "phase": "counting",
                "cursor_log_id": last_id,
                "etl_last_egmid": last_egmid,
            }
        )
        log("Инкремент журнала EXCHANGELOG; COUNT в Firebird для прогресса не выполняется.")
        _raise_if_cancel(cancel_check)
        total_exchangelog = _count_exchangelog_total()

        detail(
            {
                "phase": "exchangelog_ready",
                "loaded_rows": 0,
                "parsed_facts": 0,
                "journal_facts": 0,
                "staging_errors": 0,
                "cursor_log_id": last_id,
                "etl_last_egmid": last_egmid,
                **_progress_payload_total_rows(total_exchangelog),
            }
        )

        page_stats = _sync_journal_snapshot_interleaved(
            cfg,
            pg,
            base_sql=base_sql,
            last_id=last_id,
            total_exchangelog=total_exchangelog,
            progress_detail_cb=progress_detail_cb,
            log=log,
            detail=detail,
            cancel_check=cancel_check,
            best_lic_by_jid=best_lic_by_jid or None,
            mo_uid_to_jid=mo_uid_to_jid or None,
        )

        _raise_if_cancel(cancel_check)
        detail(
            {
                "phase": "exchangelog_done",
                "loaded_rows": page_stats.fetched,
                "parsed_facts": page_stats.facts,
                "journal_facts": page_stats.facts,
                "staging_errors": page_stats.staging_n,
                "cursor_log_id": page_stats.last_id,
                **_progress_payload_total_rows(total_exchangelog),
            }
        )

        if pg is not None and not dry_run:
            _refresh_outbound_documents(
                cfg,
                pg,
                progress_state={
                    "total_exchangelog": total_exchangelog,
                    "fetched": page_stats.fetched,
                    "facts": page_stats.facts,
                    "staging_n": page_stats.staging_n,
                },
                progress_detail_cb=progress_detail_cb,
                log=log,
                detail=detail,
                cancel_check=cancel_check,
            )
            # last_egmid — курсор выгрузки снимка EGISZ_MESSAGES (аналог last_log_id для журнала).
            # messages_snapshot_high_egmid дублирует тот же курсор для совместимости со схемой/диагностикой.
            set_etl_source_peaks(pg, pipeline, None, reference_max_license_modifydate)
            snap_hi = int(page_stats.messages_snapshot_scan_high_egmid)
            set_last_egmid(pg, pipeline, snap_hi)
            set_messages_snapshot_high_egmid(pg, pipeline, snap_hi)
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
