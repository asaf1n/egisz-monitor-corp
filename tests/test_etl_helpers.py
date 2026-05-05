"""Юнит-тесты для расщеплённых хелперов run_sync и advisory lock helpers."""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from egisz_monitor_corp.config_loader import (
    AutoSyncConfig,
    CorpAppConfig,
    EtlConfig,
    FirebirdConfig,
    PostgresConfig,
)
from egisz_monitor_corp.etl import (
    EtlCancelledError,
    _count_exchangelog_total,
    _egmid_sql_int,
    _ensure_exchangelog_msgids_in_staging_from_firebird,
    _is_test_clinic,
    _messages_journal_full_rescan,
    _process_exchangelog_pages,
    _sync_window_rescan_each_run,
    _to_int,
)
from egisz_monitor_corp.pg_warehouse import (
    _pipeline_lock_key,
    release_pipeline_lock,
    try_acquire_pipeline_lock,
)


def _cfg(sync_window_days: int = 30, source_query: str | None = None) -> CorpAppConfig:
    return CorpAppConfig(
        firebird=FirebirdConfig(
            host="x", port=3050, database="x", user="u", password="p", charset="WIN1251"
        ),
        postgres=PostgresConfig(
            host="pg", port=5432, database="db", user="u", password="p", schema="public"
        ),
        etl=EtlConfig(
            batch_size=500,
            pipeline_name="firebird_exchangelog",
            sync_window_days=sync_window_days,
            source_query=source_query,
        ),
        metabase={},
        auto_sync=AutoSyncConfig(),
    )


def test_messages_journal_full_rescan_when_sync_window_negative() -> None:
    assert _messages_journal_full_rescan(_cfg(sync_window_days=0)) is False
    assert _messages_journal_full_rescan(_cfg(sync_window_days=-1)) is True
    assert _messages_journal_full_rescan(_cfg(sync_window_days=30)) is False


def test_sync_window_rescan_each_run_only_when_positive_window() -> None:
    assert _sync_window_rescan_each_run(_cfg(sync_window_days=0)) is False
    assert _sync_window_rescan_each_run(_cfg(sync_window_days=-1)) is False
    assert _sync_window_rescan_each_run(_cfg(sync_window_days=30)) is True


def test_count_exchangelog_total_is_zero_without_firebird() -> None:
    assert _count_exchangelog_total() == 0


def test_is_test_clinic_matches_ru_and_en() -> None:
    assert _is_test_clinic("ТЕСТ Иванов") is True
    assert _is_test_clinic("test clinic") is True
    assert _is_test_clinic("Поликлиника №1") is False
    assert _is_test_clinic(None) is False
    assert _is_test_clinic("") is False


def test_pipeline_lock_key_is_deterministic_int64() -> None:
    a = _pipeline_lock_key("firebird_exchangelog")
    b = _pipeline_lock_key("firebird_exchangelog")
    assert a == b
    assert _pipeline_lock_key("firebird_exchangelog") != _pipeline_lock_key("other_pipeline")
    for name in ["a", "very-long-pipeline-name-12345", "русский_пайплайн"]:
        v = _pipeline_lock_key(name)
        assert -(1 << 63) <= v <= (1 << 63) - 1


class _FakeCursor:
    def __init__(self, conn: "_FakeAdvisoryConn") -> None:
        self._conn = conn
        self._row: tuple[Any, ...] | None = None

    def __enter__(self) -> "_FakeCursor":
        return self

    def __exit__(self, *_: object) -> None:
        return None

    def execute(self, sql: str, params: Any = None) -> None:
        self._conn.queries.append((sql.strip(), params))
        if "pg_try_advisory_lock" in sql:
            self._row = (self._conn.try_lock_returns,)
        elif "pg_advisory_unlock" in sql:
            self._row = (True,)
        else:
            self._row = None

    def fetchone(self) -> tuple[Any, ...] | None:
        return self._row


class _FakeAdvisoryConn:
    def __init__(self, *, try_lock_returns: bool = True) -> None:
        self.queries: list[tuple[str, Any]] = []
        self.commits = 0
        self.rollbacks = 0
        self.try_lock_returns = try_lock_returns

    def cursor(self) -> _FakeCursor:
        return _FakeCursor(self)

    def commit(self) -> None:
        self.commits += 1

    def rollback(self) -> None:
        self.rollbacks += 1


def test_try_acquire_pipeline_lock_returns_true_when_pg_returns_true() -> None:
    con = _FakeAdvisoryConn(try_lock_returns=True)
    assert try_acquire_pipeline_lock(con, "firebird_exchangelog") is True
    assert any("pg_try_advisory_lock" in q[0] for q in con.queries)
    assert con.commits == 1


def test_try_acquire_pipeline_lock_returns_false_when_busy() -> None:
    con = _FakeAdvisoryConn(try_lock_returns=False)
    assert try_acquire_pipeline_lock(con, "firebird_exchangelog") is False


def test_release_pipeline_lock_calls_unlock_and_commits() -> None:
    con = _FakeAdvisoryConn()
    release_pipeline_lock(con, "firebird_exchangelog")
    assert any("pg_advisory_unlock" in q[0] for q in con.queries)
    assert con.commits == 1


def test_to_int_rejects_zero_but_egmid_sql_int_keeps_zero_for_cursor() -> None:
    assert _to_int(0) is None
    assert _egmid_sql_int(0) == 0


def test_process_exchangelog_pages_cooperative_cancel_between_firebird_pages() -> None:
    """Отмена в начале второй итерации цикла — второй SELECT журнала не выполняется."""
    exports: list[int] = []

    def fake_export(*_a: Any, **_k: Any) -> list[dict[str, Any]]:
        exports.append(1)
        return [
            {
                "logid": 1,
                "logtext": "",
                "msgtext": "",
                "msgid": None,
                "log_created_at": None,
            }
        ]

    stops = {"n": 0}

    def cancel() -> None:
        stops["n"] += 1
        if stops["n"] >= 2:
            raise EtlCancelledError("stop")

    cfg = _cfg()
    cfg.etl.batch_size = 1
    pg = MagicMock()

    with patch("egisz_monitor_corp.etl._export_exchangelog_page", side_effect=fake_export):
        with pytest.raises(EtlCancelledError):
            _process_exchangelog_pages(
                cfg,
                pg,
                base_sql="SELECT 1",
                last_id=0,
                total_exchangelog=0,
                progress_detail_cb=None,
                log=lambda _m: None,
                detail=lambda _p: None,
                cancel_check=cancel,
            )
    assert len(exports) == 1


def test_process_exchangelog_pages_msgtext_too_large_staging_only() -> None:
    """Строка с MSGTEXT больше max_msgtext_bytes не вызывает parse_xml; staging MSGTEXT_TOO_LARGE."""
    huge = "ы" * 500  # UTF-8: 1000 байт
    rows = [
        {
            "logid": 1,
            "logtext": "",
            "msgtext": huge,
            "msgid": None,
            "log_created_at": None,
        }
    ]

    def fake_export(*_a: Any, **_k: Any) -> list[dict[str, Any]]:
        return rows

    flushed: list[list[tuple[Any, ...]]] = []
    pg = MagicMock()

    def capture_insert(_con: Any, buf: list[tuple[Any, ...]]) -> None:
        flushed.append(list(buf))

    cfg = _cfg()
    cfg.etl.max_msgtext_bytes = 100

    with (
        patch("egisz_monitor_corp.etl._export_exchangelog_page", side_effect=fake_export),
        patch("egisz_monitor_corp.etl.insert_staging_channel_errors", side_effect=capture_insert),
        patch("egisz_monitor_corp.etl.fetch_journal_messages_by_msgids", return_value=[]),
    ):
        stats = _process_exchangelog_pages(
            cfg,
            pg,
            base_sql="SELECT 1",
            last_id=0,
            total_exchangelog=1,
            progress_detail_cb=None,
            log=lambda _m: None,
            detail=lambda _p: None,
        )

    assert stats.facts == 0
    assert any(any(t[1] == "MSGTEXT_TOO_LARGE" for t in batch) for batch in flushed)


def test_ensure_exchangelog_msgids_fetches_firebird_when_missing_in_staging() -> None:
    """Недостающие MSGID из пакета журнала — один SELECT в Firebird и вставка в staging."""
    cfg = _cfg()
    pg = MagicMock()
    journal_rows = [{"msgid": "abc-1", "logid": 10, "logtext": "", "msgtext": "", "log_created_at": None}]
    fb_row = {
        "msgid": "abc-1",
        "egmid": 99,
        "replyto": None,
        "documentid": "DOC",
        "msg_created_at": None,
    }
    with (
        patch("egisz_monitor_corp.etl.journal_msgids_present_in_staging", return_value=set()),
        patch("egisz_monitor_corp.etl._etl_fb_fetch", return_value=[fb_row]) as mock_fb,
        patch("egisz_monitor_corp.etl.insert_journal_messages_staging_rows") as mock_ins,
    ):
        _ensure_exchangelog_msgids_in_staging_from_firebird(
            cfg, pg, journal_rows, log=lambda _m: None, cancel_check=None
        )
    mock_fb.assert_called_once()
    mock_ins.assert_called_once_with(pg, [fb_row])
