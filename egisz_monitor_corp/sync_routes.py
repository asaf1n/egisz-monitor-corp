"""Background Firebird->Postgres sync from the web UI (single-flight)."""

from __future__ import annotations

import threading
from pathlib import Path
from typing import Any, Callable

_state_lock = threading.Lock()
_state: dict[str, Any] = {
    "running": False,
    "message": "",
    "error": None,
    "last_stats": None,
    "progress": None,
}


def _run_sync_job(config_path: str, merged_dict: dict[str, Any] | None = None) -> None:
    def log(m: str) -> None:
        with _state_lock:
            _state["message"] = m

    try:
        import os

        os.environ["EGISZ_MONITOR_CONFIG"] = config_path
        from egisz_monitor_corp.config_loader import load_corp_config, parse_corp_config_dict
        from egisz_monitor_corp.etl import run_sync
        from egisz_monitor_corp.pg_warehouse import PipelineLockBusyError

        if merged_dict is not None:
            cfg = parse_corp_config_dict(merged_dict)
        else:
            cfg = load_corp_config()

        def on_progress_detail(payload: dict[str, Any]) -> None:
            with _state_lock:
                _state["progress"] = payload

        stats = run_sync(
            cfg,
            dry_run=False,
            progress_cb=log,
            progress_detail_cb=on_progress_detail,
        )
        with _state_lock:
            _state["last_stats"] = {
                "fetched": stats.fetched,
                "facts_upserted": stats.facts_upserted,
                "staging_errors": stats.staging_errors,
                "max_log_id": stats.max_log_id,
                "cursor_after": stats.last_cursor_after,
            }
            _state["error"] = None
            _state["message"] = "Готово."
    except PipelineLockBusyError as e:  # pragma: no cover - конфликт CronJob ↔ UI
        with _state_lock:
            _state["error"] = str(e)
            _state["message"] = (
                "Параллельный sync уже идёт (возможно, его запустил CronJob egisz-monitor-sync). "
                "Подождите 1–2 минуты и повторите."
            )
    except Exception as e:  # pragma: no cover
        with _state_lock:
            _state["error"] = str(e)
            _state["message"] = f"Ошибка: {e}"
    finally:
        with _state_lock:
            _state["running"] = False


def try_start_sync(
    config_path: str, merged_dict: dict[str, Any] | None = None
) -> tuple[bool, str]:
    with _state_lock:
        if _state["running"]:
            return False, "Синхронизация уже выполняется."
        _state["running"] = True
        _state["error"] = None
        _state["message"] = "Запуск..."
        _state["last_stats"] = None
        _state["progress"] = None
    t = threading.Thread(
        target=_run_sync_job, args=(config_path, merged_dict), daemon=True
    )
    t.start()
    return True, "Синхронизация запущена в фоне."


def get_sync_state() -> dict[str, Any]:
    with _state_lock:
        return {
            "running": _state["running"],
            "message": _state["message"],
            "error": _state["error"],
            "last_stats": _state["last_stats"],
            "progress": _state["progress"],
        }


def register_sync_routes(
    app: Any,
    config_path_resolver: Callable[[], Path],
    form_merger: Callable[[Path, Any], dict[str, Any]] | None = None,
) -> None:
    from flask import jsonify, request

    @app.post("/api/sync/start")
    def api_sync_start():  # type: ignore[no-untyped-def]
        p = config_path_resolver()
        if not p.is_file():
            return jsonify({"ok": False, "error": "config missing", "message": "Нет файла конфигурации."}), 400

        merged: dict[str, Any] | None = None
        if form_merger is not None and request.form and (
            "fb_host" in request.form or "pg_host" in request.form
        ):
            try:
                merged = form_merger(p, request.form)
                from egisz_monitor_corp.config_loader import parse_corp_config_dict

                parse_corp_config_dict(merged)
            except (ValueError, TypeError) as e:
                return jsonify(
                    {
                        "ok": False,
                        "error": str(e),
                        "message": f"Проверьте поля формы (числа, обязательные значения): {e}",
                    }
                ), 400
            except Exception as e:  # pragma: no cover
                return jsonify(
                    {
                        "ok": False,
                        "error": str(e),
                        "message": f"Конфигурация из формы не читается: {e}",
                    }
                ), 400

        ok, msg = try_start_sync(str(p), merged)
        return jsonify({"ok": ok, "message": msg})

    @app.get("/api/sync/status")
    def api_sync_status():  # type: ignore[no-untyped-def]
        return jsonify(get_sync_state())
