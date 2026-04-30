"""Юнит-тесты для расщеплённых хелперов run_sync и advisory lock helpers.

Покрытие:
1. `_load_enrichment_cache` — кэш справочников Firebird (mock fetch_all).
2. `_count_exchangelog_total` — выбор COUNT-SQL и graceful degrade при ошибке FB.
3. `_pipeline_lock_key` — детерминированный bigint в диапазоне int64.
4. `try_acquire_pipeline_lock` / `release_pipeline_lock` — корректная логика на FakeConn.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import patch

import pytest

from egisz_monitor_corp.config_loader import (
    CorpAppConfig,
    EtlConfig,
    FirebirdConfig,
    PostgresConfig,
)
from egisz_monitor_corp.etl import (
    _count_exchangelog_total,
    _is_test_clinic,
    _load_enrichment_cache,
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
            full_scan=False,
            source_query=source_query,
        ),
        metabase={},
    )


def test_load_enrichment_cache_builds_mo_uid_and_jname_maps() -> None:
    licenses = [
        {"jid": 12, "mo_uid": "1.2.3", "jname": "Клиника A", "egisz_licenses_kind": "12"},
        {"jid": 7, "mo_uid": "4.5.6", "jname": None, "egisz_licenses_kind": "31"},
        {"jid": None, "mo_uid": "x", "jname": "skip"},
    ]
    jpersons = [
        {"jid": 12, "jname": "Клиника A полное", "jinn": "1234567890", "fir_oid": "1.2.3.4"},
        {"jid": 99, "jname": "Никогда не встречается"},
    ]

    def fake_fetch(_cfg: Any, sql: str) -> list[dict[str, Any]]:
        if "EGISZ_LICENSES" in sql.upper() and "JOIN JPERSONS" in sql.upper():
            return licenses
        if "JPERSONS" in sql.upper():
            return jpersons
        return []

    with patch("egisz_monitor_corp.etl.fetch_all", side_effect=fake_fetch):
        cache = _load_enrichment_cache(_cfg(), log=lambda _m: None)

    assert cache.mo_uid_to_jid_from_egisz_licenses == {"1.2.3": 12, "4.5.6": 7}
    assert cache.jname_by_jid[12] == "Клиника A полное"
    assert cache.jname_by_jid[99] == "Никогда не встречается"
    assert cache.jpersons_by_jid[12] == ("Клиника A полное", "1234567890", "1.2.3.4")
    assert any(c[0] == 12 for c in cache.clinics)
    assert any(c[0] == 7 for c in cache.clinics)


def test_count_exchangelog_total_returns_int_or_zero_on_failure() -> None:
    def ok_fetch(_cfg: Any, sql: str) -> list[dict[str, Any]]:
        assert "COUNT(*)" in sql
        return [{"cnt": 1234}]

    with patch("egisz_monitor_corp.etl.fetch_all", side_effect=ok_fetch):
        assert _count_exchangelog_total(
            _cfg(), "SELECT 1 FROM x", has_custom_query=False, last_id=0, log=lambda _m: None
        ) == 1234

    def bad_fetch(_cfg: Any, _sql: str) -> list[dict[str, Any]]:
        raise RuntimeError("FB down")

    captured: list[str] = []
    with patch("egisz_monitor_corp.etl.fetch_all", side_effect=bad_fetch):
        n = _count_exchangelog_total(
            _cfg(),
            "SELECT * FROM EXCHANGELOG WHERE 1=1",
            has_custom_query=True,
            last_id=42,
            log=captured.append,
        )
    assert n == 0
    assert any("COUNT" in m for m in captured)


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
    # bigint signed int64 диапазон.
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
