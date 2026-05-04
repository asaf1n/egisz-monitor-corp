"""Unit tests for Metabase JSON export helpers."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from egisz_monitor_corp import metabase_export as me


def test_api_sends_x_api_key_when_env_set(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("METABASE_API_KEY", "mb_test_key_123")
    captured: dict[str, str] = {}

    class _Resp:
        def read(self) -> bytes:
            return b"{}"

        def __enter__(self) -> "_Resp":
            return self

        def __exit__(self, *args: object) -> None:
            return None

    def fake_urlopen(req: object, timeout: int = 120) -> _Resp:
        r = req  # urllib.request.Request
        for k, v in r.header_items():  # type: ignore[attr-defined]
            captured[k.lower()] = v
        return _Resp()

    monkeypatch.setattr(me.urllib.request, "urlopen", fake_urlopen)
    out = me._api("http://example.test", "/api/user/current", "GET", None, None)
    assert out == {}
    assert captured.get("x-api-key") == "mb_test_key_123"


def test_api_session_post_does_not_send_api_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("METABASE_API_KEY", "should_not_be_used_for_login")
    captured: dict[str, str] = {}

    class _Resp:
        def read(self) -> bytes:
            return b'{"id": "sess-token"}'

        def __enter__(self) -> "_Resp":
            return self

        def __exit__(self, *args: object) -> None:
            return None

    def fake_urlopen(req: object, timeout: int = 120) -> _Resp:
        for k, v in req.header_items():  # type: ignore[attr-defined]
            captured[k.lower()] = v
        return _Resp()

    monkeypatch.setattr(me.urllib.request, "urlopen", fake_urlopen)
    out = me._api("http://example.test", "/api/session", "POST", {"username": "a", "password": "b"})
    assert out["id"] == "sess-token"
    assert "x-api-key" not in captured


def test_build_export_zip_bytes_falls_back_to_bundled(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    (tmp_path / "z.json").write_text("{}", encoding="utf-8")
    monkeypatch.setenv("METABASE_STATIC_DASHBOARDS_DIR", str(tmp_path))

    def boom(*a, **k):
        raise RuntimeError("live failed")

    monkeypatch.setattr(me, "export_dashboards_zip", boom)
    blob, fn, source = me.build_export_zip_bytes()
    assert source == "bundled"
    assert "bundled" in fn
    assert blob[:2] == b"PK"


def test_build_static_bundled_dashboards_zip(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    import io
    import zipfile

    (tmp_path / "01_x.json").write_text('{"name":"x","cards":[]}', encoding="utf-8")
    (tmp_path / "field_filter_defaults.yaml").write_text("a: 1\n", encoding="utf-8")
    monkeypatch.setenv("METABASE_STATIC_DASHBOARDS_DIR", str(tmp_path))
    blob, fn = me.build_static_bundled_dashboards_zip()
    assert "bundled" in fn
    zf = zipfile.ZipFile(io.BytesIO(blob))
    names = sorted(zf.namelist())
    assert "01_x.json" in names
    assert "field_filter_defaults.yaml" in names


def test_safe_zip_entry_name() -> None:
    assert me._safe_zip_entry_name(8, "08 Архив СЭМД").startswith("8_")
    assert me._safe_zip_entry_name(1, "x").endswith(".json")


def test_existing_dashboard_name_to_filename(tmp_path: Path) -> None:
    p = tmp_path / "01_operational.json"
    p.write_text(json.dumps({"name": "01 Оперативный мониторинг", "cards": []}), encoding="utf-8")
    m = me._existing_dashboard_name_to_filename(str(tmp_path))
    assert m["01 Оперативный мониторинг"] == "01_operational.json"
