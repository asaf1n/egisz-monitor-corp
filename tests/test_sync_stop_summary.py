"""Итог кооперативной остановки ETL: last_stats не пустой (регрессия против «нет счётчиков» в UI)."""

from egisz_monitor_corp.sync_routes import _compose_stop_summary_stats


def test_compose_stop_summary_records_progress_snapshot_without_pg() -> None:
    detail = {
        "phase": "exchangelog_parse",
        "cursor_log_id": 29_048_222,
        "etl_last_egmid": 87_581_003,
        "loaded_rows": 9000,
        "page": 13,
    }
    out = _compose_stop_summary_stats(None, detail)
    assert out["stopped_by_user"] is True
    assert "note_ru" in out
    assert out["progress_phase"] == "exchangelog_parse"
    assert out["progress_cursor_log_id"] == 29_048_222
    assert out["progress_loaded_rows"] == 9000


def test_compose_stop_summary_empty_detail_still_has_flag() -> None:
    out = _compose_stop_summary_stats(None, None)
    assert out["stopped_by_user"] is True
