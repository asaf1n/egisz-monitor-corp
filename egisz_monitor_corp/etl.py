"""Firebird → PostgreSQL ETL for corp fact table (LOGID cursor, not MODIFYDATE)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

from egisz_monitor_corp.config_loader import CorpAppConfig, load_corp_config
from egisz_monitor_corp.fb_client import fetch_all
from egisz_monitor_corp.parser import EgiszMonitorParser, NormalizedRecord, StagingParseError
from egisz_monitor_corp.pg_warehouse import (
    apply_sql_files,
    connect_pg,
    ensure_etl_state_table,
    get_last_log_id,
    insert_staging_errors,
    set_last_log_id,
    upsert_dim_clinic,
    upsert_dim_semd,
    upsert_facts_batch,
)
from egisz_monitor_corp.sql_util import default_exchangelog_select, enrichment_licenses_sql, paginated_exchangelog_sql


@dataclass
class EtlRunStats:
    fetched: int
    facts_upserted: int
    staging_errors: int
    max_log_id: int
    last_cursor_after: int


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


def run_sync(
    cfg: CorpAppConfig | None = None,
    *,
    dry_run: bool = False,
    progress_cb: Callable[[str], None] | None = None,
) -> EtlRunStats:
    """
    Load enrichment from Firebird, paginate EXCHANGELOG by LOGID, parse LOGTEXT, UPSERT PG.
    """
    cfg = cfg or load_corp_config()

    def log(msg: str) -> None:
        if progress_cb:
            progress_cb(msg)

    base_sql = (cfg.etl.source_query or "").strip() or default_exchangelog_select(cfg.etl.sync_window_days)
    pipeline = cfg.etl.pipeline_name
    batch = max(1, cfg.etl.batch_size)

    license_map: dict[str, int] = {}
    clinics: list[tuple[int, str | None, str | None]] = []

    log("Fetching EGISZ_LICENSES + JPERSONS from Firebird...")
    lic_rows = fetch_all(cfg.firebird, enrichment_licenses_sql())
    for r in lic_rows:
        mo = _to_str(r.get("mo_uid"))
        jid = _to_int(r.get("jid"))
        if mo and jid:
            license_map[mo] = jid
        if jid:
            clinics.append((jid, _to_str(r.get("jname")), mo))

    pg = None if dry_run else connect_pg(cfg.postgres)
    staging_buffer: list[tuple[str | None, str, str, str | None]] = []
    fact_buffer: list[dict[str, Any]] = []
    fetched = 0
    facts = 0
    staging_n = 0
    max_log_id = 0

    try:
        if pg is not None:
            apply_sql_files(pg, "001_schema.sql", "002_etl_state.sql")
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
            rows = fetch_all(cfg.firebird, sql)
            if not rows:
                break
            fetched += len(rows)

            for r in rows:
                lid = _to_int(r.get("logid"))
                if lid and lid > max_log_id:
                    max_log_id = lid

                logtext = r.get("logtext")
                if logtext is not None and not isinstance(logtext, str):
                    logtext = str(logtext)

                kind_lic = r.get("license_kind")
                mo_uid = r.get("mo_uid")
                lic_jid = _to_int(r.get("license_jid"))

                rec = parser.build_record(
                    logtext,
                    kind_from_licenses=kind_lic,
                    org_from_licenses=_to_str(mo_uid),
                    license_jid_from_row=lic_jid,
                    license_jid_by_mo_uid=license_map,
                    on_staging_error=on_stage,
                )
                if rec is None:
                    continue
                fact_buffer.append(rec.as_fact_row())
                facts += 1

                if rec.kind_code and rec.kind_name:
                    if pg is not None:
                        upsert_dim_semd(pg, rec.kind_code, rec.kind_name)

                if rec.jid and rec.jid > 0:
                    jn = None
                    for jid, jname, mouid in clinics:
                        if jid == rec.jid:
                            jn = jname
                            break
                    if pg is not None:
                        upsert_dim_clinic(pg, rec.jid, jn, _to_str(mo_uid) or rec.org_oid)

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

            if len(rows) < batch:
                break

        if pg is not None and fact_buffer:
            upsert_facts_batch(pg, fact_buffer)
            pg.commit()
        if pg is not None and staging_buffer:
            insert_staging_errors(pg, staging_buffer)
            pg.commit()

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
