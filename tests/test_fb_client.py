"""Tests for Firebird client helpers."""

from __future__ import annotations

from datetime import datetime
from unittest.mock import patch

from egisz_monitor_corp.config_loader import FirebirdConfig
from egisz_monitor_corp.fb_client import fetch_firebird_source_peaks


def _fb_cfg() -> FirebirdConfig:
    return FirebirdConfig(
        host="127.0.0.1",
        port=3050,
        database="/tmp/x.fdb",
        user="SYSDBA",
        password="x",
    )


def test_fetch_firebird_source_peaks_ok() -> None:
    dt = datetime(2024, 6, 15, 10, 30, 0)
    with patch("egisz_monitor_corp.fb_client.fetch_all") as fa:
        fa.side_effect = [
            [{"max_egmid": 29261989}],
            [{"max_licenses_modifydate": dt}],
        ]
        out = fetch_firebird_source_peaks(_fb_cfg())
    assert fa.call_count == 2
    assert out["error"] is None
    assert out["max_egmid"] == 29261989
    assert out["max_licenses_modifydate"] == "2024-06-15T10:30:00"


def test_fetch_firebird_source_peaks_error() -> None:
    with patch("egisz_monitor_corp.fb_client.fetch_all", side_effect=RuntimeError("no fb")):
        out = fetch_firebird_source_peaks(_fb_cfg())
    assert out["max_egmid"] is None
    assert out["max_licenses_modifydate"] is None
    assert "no fb" in (out.get("error") or "")
