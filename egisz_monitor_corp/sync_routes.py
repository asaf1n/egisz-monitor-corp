"""Background Firebird->Postgres sync from the web UI (single-flight)."""

from __future__ import annotations

import threading
from typing import Any

_state: dict[str, Any] = {
    "running": False,
    "message": "",
    "error": None,
    "last_stats": None,
}


def _run_sync_job(config_path: str) -> None:
    def log(m: str) -> None:
        _state["message"] = m

    try:
        import os

        os.environ["EGISZ_CORP_CONFIG"] = config_path
        from egisz_monitor_corp.config_loader import load_corp_config
        from egisz_monitor_corp.etl import run_sync

        cfg = load_corp_config()
        stats = run_sync(cfg, dry_run=False, progress_cb=log)
        _state["last_stats"] = {
            "fetched": stats.fetched,
            "facts_upserted": stats.facts_upserted,
            "staging_errors": stats.staging_errors,
            "max_log_id": stats.max_log_id,
            "cursor_after": stats.last_cursor_after,
        }
        _state["error"] = None
        _state["message"] = "Готово."
    except Exception as e:  # pragma: no cover
        _state["error"] = str(e)
        _state["message"] = f"Ошибка: {e}"
    finally:
        _state["running"] = False


def try_start_sync(config_path: str) -> tuple[bool, str]:
    if _state["running"]:
        return False, "Синхронизация уже выполняется."
    _state["running"] = True
    _state["error"] = None
    _state["message"] = "Запуск..."
    _state["last_stats"] = None
    t = threading.Thread(target=_run_sync_job, args=(config_path,), daemon=True)
    t.start()
    return True, "Синхронизация запущена в фоне."


def get_sync_state() -> dict[str, Any]:
    return {
        "running": _state["running"],
        "message": _state["message"],
        "error": _state["error"],
        "last_stats": _state["last_stats"],
    }


def register_sync_routes(app: Any, config_path_resolver: Any) -> None:
    from flask import jsonify

    @app.post("/api/sync/start")
    def api_sync_start():  # type: ignore[no-untyped-def]
        p = config_path_resolver()
        if not p.is_file():
            return jsonify({"ok": False, "error": "config missing"}), 400
        ok, msg = try_start_sync(str(p))
        return jsonify({"ok": ok, "message": msg})

    @app.get("/api/sync/status")
    def api_sync_status():  # type: ignore[no-untyped-def]
        return jsonify(get_sync_state())
