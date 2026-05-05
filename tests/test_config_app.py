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
            "etl_cursor_egmid": 29261989,
        },
        "level_summary": {"red": 1, "yellow": 0, "green": 1},
        "errors": [],
    }
    fb_lic = {
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
        return_value={"source_max_licenses_modifydate": None},
    ), patch(
        "egisz_monitor_corp.config_app.fetch_firebird_max_license_modifydate", return_value=fb_lic
    ):
        resp = client.get("/api/healthcheck")

    assert resp.status_code == 200
    data = resp.get_json()
    assert data["ok"] is True
    assert data["level_summary"]["red"] == 1
    assert data["signals"][0]["code"] == "error_rate_high"
    assert data["by_clinic_top"][0]["jid"] == 12

    proxy = data["proxy_db"]
    assert proxy["etl_cursor_egmid"] == 29261989
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


def test_index_html_uses_null_safe_bind_click(cfg_yaml: Path) -> None:
    """Раньше document.getElementById('…').onclick = … ронял весь <script>, если элемента не было — не работали все кнопки."""
    app = create_app()
    app.testing = True
    resp = app.test_client().get("/")
    assert resp.status_code == 200
    html = resp.get_data(as_text=True)
    assert "function bindClick" in html
    assert "bindClick('btnSaveYaml'" in html
    assert "bindClick('btnSync'" in html
    assert "bindClick('btnSyncStop'" in html
    assert 'id="rightAsideShell"' in html
    assert 'id="btnRightAsideToggle"' in html
    assert "function formatSyncStatusBlock" in html
    assert "function loadPgSyncSnapshotOnce" in html
    assert "function etlStatusOneLine" in html
    assert "function statusLinePhrase" in html
    assert "function connStatusStripState" in html
    assert "conn-strip-stop-hazard" in html
    assert "repeating-linear-gradient" in html
    assert "function buildSystemLogText" in html
    assert 'name="etl_batch"' in html
    assert 'name="etl_sync_days"' in html
    assert "auto_sync_schedule_cron" in html
    assert "document.getElementById('btnSaveYaml').onclick" not in html
    assert "document.getElementById('btnPgBackup').onclick" not in html
    assert '/api/sync/stop' in html


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


def test_api_sync_stop_when_idle(cfg_yaml: Path) -> None:
    app = create_app()
    app.testing = True
    resp = app.test_client().post("/api/sync/stop")
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["ok"] is False
    assert "не выполняется" in body["message"].lower()


def test_api_sync_stop_when_running_accepts_cancel(cfg_yaml: Path) -> None:
    """Последняя остановка: при активном синке POST /api/sync/stop принимает кооперативный cancel."""
    import egisz_monitor_corp.sync_routes as sr

    app = create_app()
    app.testing = True
    client = app.test_client()
    with sr._state_lock:
        sr._state["running"] = True
        sr._cancel_evt.clear()
    try:
        resp = client.post("/api/sync/stop")
        assert resp.status_code == 200
        body = resp.get_json()
        assert body["ok"] is True
        low = body["message"].lower()
        assert "останов" in low or "стоп" in low or "выйдет" in low
        assert sr._cancel_evt.is_set()
    finally:
        with sr._state_lock:
            sr._state["running"] = False
            sr._cancel_evt.clear()


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
        "auto_sync_schedule_cron": "*/15 * * * *",
        "auto_sync_timezone": "Etc/UTC",
    }


def test_save_applies_cronjob_reconcile(cfg_yaml: Path) -> None:
    app = create_app()
    app.testing = True
    client = app.test_client()
    form = {**_full_config_form(), "auto_sync_enabled": "1"}
    with patch(
        "egisz_monitor_corp.config_app.reconcile_egisz_monitor_sync_cronjob",
        return_value=(True, "CronJob egisz-monitor-sync: suspend=False, schedule='*/15 * * * *', timeZone='Etc/UTC'"),
    ) as rmock:
        resp = client.post("/save", data=form)
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["ok"] is True
    assert "batch_size=500" in body.get("message", "")
    assert "sync_window_days=30" in body.get("message", "")
    assert body["cronjob_reconcile"]["ok"] is True
    rmock.assert_called_once()
    call_auto = rmock.call_args[0][0]
    assert call_auto["enabled"] is True


def test_save_resets_etl_cursors_when_sync_window_negative(cfg_yaml: Path) -> None:
    from unittest.mock import MagicMock, patch

    app = create_app()
    app.testing = True
    client = app.test_client()
    form = {**_full_config_form(), "etl_sync_days": "-1"}
    mock_pg = MagicMock()
    with patch(
        "egisz_monitor_corp.config_app.reconcile_egisz_monitor_sync_cronjob",
        return_value=(True, "CronJob ok"),
    ), patch("egisz_monitor_corp.config_app.connect_pg", return_value=mock_pg):
        resp = client.post("/save", data=form)
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["ok"] is True
    assert "курсор" in (body.get("message") or "").lower()
    mock_pg.commit.assert_called()


def test_save_writes_etl_even_when_yaml_had_null_etl(cfg_yaml: Path) -> None:
    import yaml

    from egisz_monitor_corp.config_app import _merged_yaml_dict_from_form

    root = yaml.safe_load(cfg_yaml.read_text(encoding="utf-8"))
    root["etl"] = None
    cfg_yaml.write_text(yaml.safe_dump(root, allow_unicode=True), encoding="utf-8")

    merged = _merged_yaml_dict_from_form(
        cfg_yaml,
        {
            **_full_config_form(),
            "etl_batch": "3333",
            "etl_sync_days": "14",
        },
    )
    assert merged["etl"]["batch_size"] == 3333
    assert merged["etl"]["sync_window_days"] == 14

    app = create_app()
    app.testing = True
    client = app.test_client()
    form = {**_full_config_form(), "etl_batch": "3333", "etl_sync_days": "14"}
    with patch(
        "egisz_monitor_corp.config_app.reconcile_egisz_monitor_sync_cronjob",
        return_value=(True, "CronJob ok"),
    ):
        resp = client.post("/save", data=form)
    assert resp.status_code == 200
    assert resp.get_json()["ok"] is True
    saved = yaml.safe_load(cfg_yaml.read_text(encoding="utf-8"))
    assert isinstance(saved.get("etl"), dict)
    assert saved["etl"]["batch_size"] == 3333
    assert saved["etl"]["sync_window_days"] == 14


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


def test_api_metabase_export_dashboards_json_zip(cfg_yaml: Path) -> None:
    from unittest.mock import patch

    app = create_app()
    app.testing = True
    client = app.test_client()
    fake_zip = b"PK\x03\x04fake"
    with patch(
        "egisz_monitor_corp.config_app.build_export_zip_bytes",
        return_value=(fake_zip, "egisz_metabase_dashboards_20260101_120000.zip", "live"),
    ):
        resp = client.post("/api/metabase/export-dashboards-json")
    assert resp.status_code == 200
    assert resp.mimetype == "application/zip"
    assert resp.data == fake_zip
    assert resp.headers.get("X-Egisz-Metabase-Export-Source") == "live"
    cd = resp.headers.get("Content-Disposition") or ""
    assert "egisz_metabase_dashboards" in cd


def test_api_metabase_export_empty_bundle_zip(cfg_yaml: Path) -> None:
    from unittest.mock import patch

    app = create_app()
    app.testing = True
    client = app.test_client()
    fake_zip = b"PK\x03\x04bundle"
    with patch(
        "egisz_monitor_corp.config_app.build_empty_metabase_bundle_zip_bytes",
        return_value=(fake_zip, "egisz_metabase_empty_bundle_20260101_120000.zip"),
    ):
        resp = client.post("/api/metabase/export-empty-bundle", data=_full_config_form())
    assert resp.status_code == 200
    assert resp.mimetype == "application/zip"
    assert resp.data == fake_zip
    cd = resp.headers.get("Content-Disposition") or ""
    assert "egisz_metabase_empty_bundle" in cd


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
