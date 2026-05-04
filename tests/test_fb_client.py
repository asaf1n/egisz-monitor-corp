"""Tests for Firebird client helpers."""

from __future__ import annotations

import time
from datetime import datetime
from unittest.mock import patch

from egisz_monitor_corp.config_loader import FirebirdConfig
from egisz_monitor_corp import fb_client
from egisz_monitor_corp.fb_client import fetch_all, fetch_firebird_max_license_modifydate


def _fb_cfg() -> FirebirdConfig:
    return FirebirdConfig(
        host="127.0.0.1",
        port=3050,
        database="/tmp/x.fdb",
        user="SYSDBA",
        password="x",
    )


def test_fetch_firebird_max_license_modifydate_ok() -> None:
    dt = datetime(2024, 6, 15, 10, 30, 0)
    with patch("egisz_monitor_corp.fb_client.fetch_all") as fa:
        fa.return_value = [{"max_licenses_modifydate": dt}]
        out = fetch_firebird_max_license_modifydate(_fb_cfg())
    assert fa.call_count == 1
    assert "EGISZ_LICENSES" in fa.call_args[0][1]
    assert out["error"] is None
    assert out["max_licenses_modifydate"] == "2024-06-15T10:30:00"


def test_fetch_firebird_max_license_modifydate_error() -> None:
    with patch("egisz_monitor_corp.fb_client.fetch_all", side_effect=RuntimeError("no fb")):
        out = fetch_firebird_max_license_modifydate(_fb_cfg())
    assert out["max_licenses_modifydate"] is None
    assert "no fb" in (out.get("error") or "")


def test_fetch_all_wait_ticks_invoke_callback() -> None:
    ticks: list[int] = []

    def slow_impl(cfg, sql, params):  # noqa: ARG001
        time.sleep(2.2)
        return [{"ok": 1}]

    with patch.object(fb_client, "_fetch_all_impl", slow_impl):
        out = fetch_all(
            _fb_cfg(),
            "SELECT 1",
            timeout_sec=5,
            wait_tick_sec=1,
            on_wait_tick=ticks.append,
        )
    assert out == [{"ok": 1}]
    assert len(ticks) >= 1
    assert ticks[-1] >= 1
