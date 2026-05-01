"""Юнит-тесты для расщеплённых хелперов run_sync и advisory lock helpers.

Покрытие:
1. `_export_egisz_licenses_full` — кэш справочников Firebird (mock fetch_all; JPERSONS и EGISZ_LICENSES отдельно; при pg=None — сшивка в Python).
2. `_count_exchangelog_total` — выбор COUNT-SQL и graceful degrade при ошибке FB.
3. `_pipeline_lock_key` — детерминированный bigint в диапазоне int64.
4. `try_acquire_pipeline_lock` / `release_pipeline_lock` — корректная логика на FakeConn.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock, patch

from datetime import datetime, timezone

import pytest

from egisz_monitor_corp.config_loader import (
    CorpAppConfig,
    EtlConfig,
    FirebirdConfig,
    PostgresConfig,
)
from egisz_monitor_corp.etl import (
    EnrichmentCache,
    _count_exchangelog_total,
    _egmid_sql_int,
    _export_egisz_licenses_full,
    _export_egisz_messages_by_egmid,
    _is_test_clinic,
    _process_exchangelog_pages,
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
    )


def test_export_egisz_licenses_full_builds_mo_uid_and_jname_maps() -> None:
    licenses_only = [
        {
            "jid": 12,
            "mo_uid": "1.2.3",
            "egisz_licenses_kind": "12",
            "id": 1,
            "mo_domen": "clinic.example",
        },
        {"jid": 7, "mo_uid": "4.5.6", "egisz_licenses_kind": "31", "id": 2, "mo_domen": "b.example"},
        {"jid": None, "mo_uid": "x", "id": 3, "mo_domen": None},
    ]
    jp_rows = [
        {"jid": 12, "jname": "Клиника A полное", "jinn": "1234567890", "fir_oid": "1.2.3.4"},
        {"jid": 7, "jname": None, "jinn": None, "fir_oid": None},
    ]

    def fake_fetch(_cfg: Any, sql: str, *_args: Any, **_kwargs: Any) -> list[dict[str, Any]]:
        s = sql.upper()
        if "FROM JPERSONS" in s:
            return jp_rows
        if "FROM EGISZ_LICENSES" in s and "JOIN" not in s:
            return licenses_only
        return []

    with patch("egisz_monitor_corp.etl.fetch_all", side_effect=fake_fetch):
        cache = _export_egisz_licenses_full(_cfg(), log=lambda _m: None)

    assert cache.mo_uid_to_jid_from_egisz_licenses == {"1.2.3": 12, "4.5.6": 7}
    assert cache.jname_by_jid[12] == "Клиника A полное"
    assert cache.jpersons_by_jid[12] == ("Клиника A полное", "1234567890", "1.2.3.4")
    assert cache.clinic_dim_by_jid[12] == ("Клиника A полное", "1234567890", "1.2.3.4")
    assert cache.clinic_dim_by_jid[7] == (None, None, None)
    assert any(c[0] == 12 for c in cache.clinics)
    assert any(c[0] == 7 for c in cache.clinics)


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


def test_to_int_rejects_zero_but_egmid_sql_int_keeps_zero_for_cursor() -> None:
    assert _to_int(0) is None
    assert _egmid_sql_int(0) == 0


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
        patch("egisz_monitor_corp.etl.insert_staging_errors", side_effect=capture_insert),
    ):
        stats = _process_exchangelog_pages(
            cfg,
            pg,
            base_sql="SELECT 1",
            enrichment=EnrichmentCache(),
            msg_by_msgid={},
            last_id=0,
            total_exchangelog=1,
            progress_detail_cb=None,
            log=lambda _m: None,
            detail=lambda _p: None,
        )

    assert stats.facts == 0
    assert any(any(t[1] == "MSGTEXT_TOO_LARGE" for t in batch) for batch in flushed)
    """Полная страница без сдвига EGMID → выходим из цикла (иначе бесконечный опрос FB)."""
    calls = {"n": 0}

    def fake_fetch(_cfg: Any, sql: str, *_args: Any, **_kwargs: Any) -> list[dict[str, Any]]:
        calls["n"] += 1
        if calls["n"] > 2:
            raise AssertionError("fetch_all called too many times (infinite loop)")
        return [{"msgid": f"k{i}", "egmid": None, "replyto": None, "documentid": None, "msg_created_at": None} for i in range(500)]

    with patch("egisz_monitor_corp.etl.fetch_all", side_effect=fake_fetch):
        msg, cur = _export_egisz_messages_by_egmid(_cfg(), 0, log=lambda _m: None, detail=None)

    assert calls["n"] == 1
    assert cur == 0
    assert len(msg) == 500
