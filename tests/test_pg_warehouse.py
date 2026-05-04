"""Unit-тесты для healthcheck-снимка PG (без реальной БД)."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any
from unittest.mock import MagicMock

from egisz_monitor_corp.pg_warehouse import fetch_etl_watermark_row, fetch_healthcheck_snapshot


class _FakeCursor:
    """Контекст-менеджер курсора. Очередь content-результатов хранится в _FakeConn (делится между курсорами)."""

    def __init__(self, conn: "_FakeConn") -> None:
        self._conn = conn
        self._current: list[Any] = []

    def __enter__(self) -> "_FakeCursor":
        return self

    def __exit__(self, *_: object) -> None:
        return None

    def execute(self, sql: str, params: Any = None) -> None:
        s = sql.strip().lower()
        self._conn.executed.append(s)
        if s.startswith("set local statement_timeout"):
            return
        # Берём очередной content-набор у соединения (общий между всеми with cursor()).
        if self._conn.scripts:
            self._current = self._conn.scripts.pop(0)
        else:
            self._current = []

    def fetchall(self) -> list[Any]:
        return list(self._current)

    def fetchone(self) -> Any:
        return self._current[0] if self._current else None


class _FakeConn:
    def __init__(self, scripts: list[list[Any]]) -> None:
        # Очередь content-результатов; каждый content execute() забирает следующий элемент.
        self.scripts = list(scripts)
        self.executed: list[str] = []
        self.rolled_back = 0

    def cursor(self) -> _FakeCursor:
        return _FakeCursor(self)

    def rollback(self) -> None:
        self.rolled_back += 1


def test_fetch_healthcheck_snapshot_aggregates_levels_and_top_clinics() -> None:
    # Каждый блок execute() в fetch_healthcheck_snapshot открывает новый cursor() и вызывает
    # execute() сначала для SET LOCAL, потом для целевого SELECT — поэтому подаём по одному
    # «контентному» сценарию на каждый with-блок, а внутри блока курсор сначала «съедает»
    # SET LOCAL, потом отдаёт content в fetchall()/fetchone().
    signals_rows = [
        ("error_rate_high", "Доля ошибок РЭМД > порога", "red", 14.5, "%", 320, "hint A"),
        ("queue_red_24h", "Очередь без ответа > 24 часов", "yellow", 12, "docs", None, "hint B"),
        ("cursor_stale", "Курсор ETL не двигался", "green", 0, "sec_since_update", None, "hint C"),
    ]
    by_clinic_rows = [
        (
            12,
            "Клиника A",
            "1234567890",
            "1.2.3",
            300,
            260,
            36,
            4,
            12.0,
            1.33,
            5,
            datetime(2026, 4, 30, 12, 0, tzinfo=timezone.utc),
            "red",
        ),
        (
            7,
            "Клиника B",
            None,
            None,
            120,
            115,
            5,
            0,
            4.16,
            0.0,
            0,
            datetime(2026, 4, 30, 11, 0, tzinfo=timezone.utc),
            "green",
        ),
    ]
    proxy_db_row = (
        4521,
        0,
        2,
        29261989,
        datetime(2026, 4, 30, 9, 30, tzinfo=timezone.utc),
        47,
        3,
        25,
        19,
        datetime(2026, 4, 30, 12, 5, tzinfo=timezone.utc),
        18000123,
        29262000,
    )

    con = _FakeConn([signals_rows, by_clinic_rows, [proxy_db_row]])
    out = fetch_healthcheck_snapshot(con, top_clinics=2)

    assert out["level_summary"] == {"red": 1, "yellow": 1, "green": 1}

    assert len(out["signals"]) == 3
    first = out["signals"][0]
    assert first["code"] == "error_rate_high"
    assert first["level"] == "red"
    assert first["value"] == 14.5
    assert first["denominator"] == 320
    assert first["value_unit"] == "%"

    assert len(out["by_clinic_top"]) == 2
    a = out["by_clinic_top"][0]
    assert a["jid"] == 12
    assert a["clinic_name"] == "Клиника A"
    assert a["error_rate_24h"] == 12.0
    assert a["pending_now"] == 5
    assert a["health_level"] == "red"
    assert a["last_seen_at"].startswith("2026-04-30")

    proxy = out["proxy_db"]
    assert proxy["stg_outbound_total"] == 4521
    assert proxy["pending_older_24h"] == 19
    assert proxy["etl_last_log_id"] == 18000123
    assert proxy["staging_max_egmid"] == 29261989
    assert proxy["etl_cursor_egmid"] == 29262000


def test_fetch_healthcheck_snapshot_fact_fallback_when_staging_empty() -> None:
    """Если outbound staging пуст, подмешиваем агрегаты из fact_egisz_transactions."""
    signals_rows: list[Any] = []
    by_clinic_rows: list[Any] = []
    proxy_empty = (0, 0, 0, None, None, 0, 0, 0, 0, None, None, None)
    fact_agg = [(1646, 12, 29261980)]

    con = _FakeConn([signals_rows, by_clinic_rows, [proxy_empty], fact_agg])
    out = fetch_healthcheck_snapshot(con, top_clinics=2)

    proxy = out["proxy_db"]
    assert proxy["stg_outbound_total"] == 0
    assert proxy["fact_rows"] == 1646
    assert proxy["fact_without_egmid"] == 12
    assert proxy["fact_max_egmid"] == 29261980


def test_fetch_healthcheck_snapshot_records_errors_per_view() -> None:
    """Если v_health_signals недоступна (например, схема не применена), снимок остаётся валидным."""
    import psycopg2

    bad_cur = MagicMock()
    bad_cur.__enter__.return_value = bad_cur
    bad_cur.__exit__.return_value = None
    side: list[Any] = []

    def execute(sql: str, params: Any = None) -> None:
        s = sql.strip().lower()
        if s.startswith("set local"):
            return
        side.append(s)
        raise psycopg2.Error("relation v_health_signals does not exist")

    bad_cur.execute.side_effect = execute

    con = MagicMock()
    con.cursor.return_value = bad_cur
    con.rollback = MagicMock()

    out = fetch_healthcheck_snapshot(con)
    assert out["signals"] == []
    assert out["by_clinic_top"] == []
    assert out["proxy_db"] == {}
    assert any("v_health_signals" in e for e in out["errors"])
    assert con.rollback.call_count >= 1


def test_fetch_etl_watermark_row_none_when_no_row() -> None:
    cur = MagicMock()
    cur.__enter__.return_value = cur
    cur.__exit__.return_value = None
    cur.fetchone.return_value = None
    con = MagicMock()
    con.cursor.return_value = cur
    assert fetch_etl_watermark_row(con, "firebird_exchangelog") is None


def test_fetch_etl_watermark_row_returns_ints() -> None:
    cur = MagicMock()
    cur.__enter__.return_value = cur
    cur.__exit__.return_value = None
    cur.fetchone.return_value = (29_614_055, 89_339_643, 89_400_000, 88_000_000)
    con = MagicMock()
    con.cursor.return_value = cur
    out = fetch_etl_watermark_row(con, "firebird_exchangelog")
    assert out == {
        "last_log_id": 29_614_055,
        "last_egmid": 89_339_643,
        "source_max_egmid": 89_400_000,
        "messages_snapshot_high_egmid": 88_000_000,
    }
