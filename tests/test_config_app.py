"""Flask test client: /api/healthcheck.

Проверяем:
1) graceful degrade при недоступной Postgres — `{"ok": false, "errors": [...]}` с 200.
2) валидный JSON-формат при моках connect_pg + fetch_healthcheck_snapshot.
"""

from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import patch

import pytest

from egisz_monitor_corp.config_app import create_app


@pytest.fixture()
def cfg_yaml(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    text = """
firebird:
  host: 127.0.0.1
  port: 3050
  database: x
  user: SYSDBA
  password: masterkey
  charset: WIN1251
postgres:
  host: pg
  port: 5432
  database: egisz_reports
  user: egisz
  password: egisz
  schema: public
etl:
  batch_size: 500
  pipeline_name: firebird_exchangelog
  sync_window_days: 30
  full_scan: false
  source_query: null
metabase:
  site_url: http://127.0.0.1:3000
"""
    p = tmp_path / "egisz_monitor.yaml"
    p.write_text(text, encoding="utf-8")
    monkeypatch.setenv("EGISZ_MONITOR_CONFIG", str(p))
    monkeypatch.setenv("CONFIG_WRITE_PATH", str(p))
    return p


def test_healthcheck_returns_signals_top_clinics_and_proxy(cfg_yaml: Path) -> None:
    app = create_app()
    app.testing = True
    client = app.test_client()

    snap = {
        "signals": [
            {
                "code": "error_rate_high",
                "title": "Доля ошибок РЭМД > порога",
                "level": "red",
                "value": 14.5,
                "value_unit": "%",
                "denominator": 320,
                "hint": "Открыть дашборд 10",
            },
            {
                "code": "cursor_stale",
                "title": "Курсор ETL не двигался",
                "level": "green",
                "value": 0.0,
                "value_unit": "sec_since_update",
                "denominator": None,
                "hint": "...",
            },
        ],
        "by_clinic_top": [
            {
                "jid": 12,
                "clinic_name": "Клиника A",
                "facts_24h": 320,
                "errors_24h": 46,
                "error_rate_24h": 14.5,
                "pending_now": 5,
                "health_level": "red",
                "last_seen_at": "2026-04-30T12:00:00+00:00",
            }
        ],
        "proxy_db": {
            "stg_outbound_total": 4521,
            "stg_without_egmid": 0,
            "staging_max_egmid": 29261980,
            "pending_older_24h": 19,
            "etl_last_log_id": 18000123,
        },
        "level_summary": {"red": 1, "yellow": 0, "green": 1},
        "errors": [],
    }
    fb_peaks = {
        "max_egmid": 29261989,
        "max_licenses_modifydate": "2026-04-29T20:00:00",
        "error": None,
    }

    class _DummyConn:
        def close(self) -> None:
            return None

    with patch("egisz_monitor_corp.config_app.connect_pg", return_value=_DummyConn()), patch(
        "egisz_monitor_corp.config_app.fetch_healthcheck_snapshot", return_value=snap
    ), patch(
        "egisz_monitor_corp.config_app.fetch_etl_source_peaks_from_pg",
        return_value={"source_max_egmid": None, "source_max_licenses_modifydate": None},
    ), patch(
        "egisz_monitor_corp.config_app.fetch_firebird_source_peaks", return_value=fb_peaks
    ):
        resp = client.get("/api/healthcheck")

    assert resp.status_code == 200
    data = resp.get_json()
    assert data["ok"] is True
    assert data["level_summary"]["red"] == 1
    assert data["signals"][0]["code"] == "error_rate_high"
    assert data["by_clinic_top"][0]["jid"] == 12

    proxy = data["proxy_db"]
    assert proxy["fb_max_egmid"] == 29261989
    assert proxy["egmid_lag"] == 9
    assert proxy["fb_max_licenses_modifydate"].startswith("2026-04-29")


def test_healthcheck_graceful_when_pg_down(cfg_yaml: Path) -> None:
    app = create_app()
    app.testing = True
    client = app.test_client()

    def _raise(*_: object, **__: object) -> None:
        raise RuntimeError("network down")

    with patch("egisz_monitor_corp.config_app.connect_pg", side_effect=_raise):
        resp = client.get("/api/healthcheck")

    assert resp.status_code == 200
    data = resp.get_json()
    assert data["ok"] is False
    assert any("PostgreSQL" in str(e) or "network" in str(e) for e in data.get("errors", []))


def test_healthcheck_404_when_no_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("EGISZ_MONITOR_CONFIG", str(tmp_path / "missing.yaml"))
    monkeypatch.setenv("CONFIG_WRITE_PATH", str(tmp_path / "missing.yaml"))
    # Сбрасываем кэшированные модульные state, если есть.
    if "egisz_monitor_corp.config_app" in os.sys.modules:
        # ничего не делаем: app создаётся фабрикой каждый тест.
        pass

    app = create_app()
    app.testing = True
    client = app.test_client()
    resp = client.get("/api/healthcheck")
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["ok"] is False
    assert "конфигурации" in (data.get("error", "")).lower()


def test_api_sync_start_rejects_invalid_form_etl_batch(cfg_yaml: Path) -> None:
    import egisz_monitor_corp.sync_routes as sr
    from unittest.mock import patch

    app = create_app()
    app.testing = True
    client = app.test_client()
    data = {
        "fb_host": "127.0.0.1",
        "fb_port": "3050",
        "fb_database": "x",
        "fb_charset": "WIN1251",
        "fb_user": "SYSDBA",
        "fb_password": "masterkey",
        "pg_host": "pg",
        "pg_port": "5432",
        "pg_database": "egisz_reports",
        "pg_schema": "public",
        "pg_user": "egisz",
        "pg_password": "egisz",
        "etl_batch": "not_int",
        "etl_sync_days": "30",
    }
    with patch.object(sr.threading, "Thread") as Tmock:
        resp = client.post("/api/sync/start", data=data)
    Tmock.assert_not_called()
    assert resp.status_code == 400
    body = resp.get_json()
    assert body["ok"] is False


def test_api_sync_start_form_merges_etl_into_thread_args(cfg_yaml: Path) -> None:
    import egisz_monitor_corp.sync_routes as sr
    from unittest.mock import patch

    class RecordingThread:
        captured: tuple[object, ...] | None = None

        def __init__(self, target=None, args=(), kwargs=None, daemon=None):
            RecordingThread.captured = tuple(args)

        def start(self) -> None:
            pass

    app = create_app()
    app.testing = True
    client = app.test_client()
    data = {
        "fb_host": "127.0.0.1",
        "fb_port": "3050",
        "fb_database": "x",
        "fb_charset": "WIN1251",
        "fb_user": "SYSDBA",
        "fb_password": "masterkey",
        "pg_host": "pg",
        "pg_port": "5432",
        "pg_database": "egisz_reports",
        "pg_schema": "public",
        "pg_user": "egisz",
        "pg_password": "egisz",
        "etl_batch": "999",
        "etl_sync_days": "30",
    }
    with patch.object(sr.threading, "Thread", RecordingThread):
        resp = client.post("/api/sync/start", data=data)
    assert resp.status_code == 200
    assert resp.get_json()["ok"] is True
    assert RecordingThread.captured is not None
    _path, merged = RecordingThread.captured
    assert isinstance(merged, dict)
    assert merged["etl"]["batch_size"] == 999


def _full_config_form() -> dict[str, str]:
    return {
        "fb_host": "127.0.0.1",
        "fb_port": "3050",
        "fb_database": "x",
        "fb_charset": "WIN1251",
        "fb_user": "SYSDBA",
        "fb_password": "masterkey",
        "pg_host": "pg",
        "pg_port": "5432",
        "pg_database": "egisz_reports",
        "pg_schema": "public",
        "pg_user": "egisz",
        "pg_password": "egisz",
        "etl_batch": "500",
        "etl_sync_days": "30",
    }


def test_api_pg_backup_streams_custom_dump(cfg_yaml: Path) -> None:
    from unittest.mock import patch

    app = create_app()
    app.testing = True
    client = app.test_client()
    with patch("egisz_monitor_corp.config_app.pg_dump_custom_bytes", return_value=b"\x01PGD"):
        resp = client.post("/api/pg/backup", data=_full_config_form())
    assert resp.status_code == 200
    assert resp.mimetype == "application/octet-stream"
    assert resp.data == b"\x01PGD"
    cd = resp.headers.get("Content-Disposition") or ""
    assert "attachment" in cd.lower()
    assert ".dump" in cd


def test_api_pg_restore_multipart_ok(cfg_yaml: Path) -> None:
    from io import BytesIO
    from unittest.mock import patch

    app = create_app()
    app.testing = True
    client = app.test_client()
    form = _full_config_form()
    with patch(
        "egisz_monitor_corp.config_app.restore_upload_to_temp_and_run",
        return_value="pg_restore: finished\n",
    ):
        resp = client.post(
            "/api/pg/restore",
            data={**form, "dump": (BytesIO(b"\x01\x02\x03"), "snap.dump")},
        )
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["ok"] is True
    assert "finished" in body["message"]


def test_api_pg_restore_rejects_missing_file(cfg_yaml: Path) -> None:
    app = create_app()
    app.testing = True
    client = app.test_client()
    resp = client.post("/api/pg/restore", data=_full_config_form())
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["ok"] is False
