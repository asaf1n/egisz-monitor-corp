"""Дедупликация снимка EGISZ_MESSAGES перед INSERT (один MSGID на команду execute_values)."""

from __future__ import annotations

from egisz_monitor_corp.pg_warehouse import _dedupe_journal_snapshot_rows_by_msgid


def test_dedupe_journal_snapshot_keeps_max_egmid_per_msgid() -> None:
    rows = [
        {"msgid": "  A  ", "egmid": 1, "replyto": "old"},
        {"msgid": "A", "egmid": 100, "replyto": "new"},
        {"msgid": "B", "egmid": 2},
    ]
    out = _dedupe_journal_snapshot_rows_by_msgid(rows)
    assert len(out) == 2
    by = {str(r["msgid"]).strip(): r for r in out}
    assert by["A"]["egmid"] == 100
    assert by["A"]["replyto"] == "new"
    assert by["B"]["egmid"] == 2


def test_dedupe_journal_snapshot_order_stable_first_seen_msgid() -> None:
    rows = [
        {"msgid": "Z", "egmid": 9},
        {"msgid": "Y", "egmid": 8},
    ]
    out = _dedupe_journal_snapshot_rows_by_msgid(rows)
    assert [r["msgid"] for r in out] == ["Z", "Y"]
