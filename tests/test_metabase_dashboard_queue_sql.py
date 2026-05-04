"""Regression: queue view has no relates_to_id column; snapshot card must use localUid СЭМД."""

import json
from pathlib import Path


def test_provision_anchor_last_operations_query_contains_bundle_fragment() -> None:
    """Синхронно с metabase/provision.sh corp_mb_native_sql_anchor_matches_image()."""
    root = Path(__file__).resolve().parents[1]
    path = root / "metabase_dashboards" / "01_operational.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    card = next(c for c in data["cards"] if c.get("name") == "Последние операции")
    anchor = "v_egisz_transactions_enriched_ui.* FROM public.v_egisz_transactions_enriched_ui"
    assert anchor in card["dataset_query"]["native"]["query"]


def test_quality_errors_per_clinic_semd_includes_relates_to_in_cte() -> None:
    """Native card must expose \"Связанное сообщение\" in CTE for COUNT(DISTINCT ...) in outer query."""
    root = Path(__file__).resolve().parents[1]
    path = root / "metabase_dashboards" / "04_quality_and_errors.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    card = next(c for c in data["cards"] if c.get("name") == "04 · Ошибки по клиникам и СЭМД")
    q = card["dataset_query"]["native"]["query"]
    assert ") AS err_summary, \"Связанное сообщение\" FROM public.v_egisz_transactions_enriched_ui" in q
    assert "COUNT(DISTINCT \"Связанное сообщение\")" in q


def test_executive_snapshot_queue_subquery_uses_local_uid_not_relates_to() -> None:
    root = Path(__file__).resolve().parents[1]
    path = root / "metabase_dashboards" / "05_executive.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    snapshot = next(c for c in data["cards"] if c.get("name") == "05 · Сводка по снимку данных")
    q = snapshot["dataset_query"]["native"]["query"]
    # Очередь без ответа: COUNT по localUid СЭМД (регрессия против COUNT по relates_to_id во view очереди).
    anchor = 'COUNT(DISTINCT p."localUid СЭМД")'
    assert anchor in q
    assert (
        "COUNT(DISTINCT \"Связанное сообщение\")::bigint FROM public.v_rpt_documents_no_response_ui"
        not in q
    )
