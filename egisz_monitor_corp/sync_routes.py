"""Background Firebird->Postgres sync from the web UI (single-flight)."""

from __future__ import annotations

import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from egisz_monitor_corp.etl import EtlCancelledError
from egisz_monitor_corp.pg_warehouse import PipelineLockBusyError

_state_lock = threading.Lock()
_state: dict[str, Any] = {
    "running": False,
    "message": "",
    "error": None,
    "last_stats": None,
    "progress": None,
    # Кольцевой буфер прогресса для UI: помогает увидеть быстрые фазы между poll'ами.
    "progress_events": [],
    "_last_progress_phase": None,
    # True после первого успешного try_start_sync в жизни процесса (до рестарта воркера).
    "sync_attempted": False,
}

_cancel_evt = threading.Event()

_SYNC_FAILED_WATERMARK_NOTE_RU = (
    "last_log_id двигается по пакетам журнала; last_egmid — ватермарк журнала после полного успешного sync. "
    "При обрыве last_log_id часто впереди last_egmid."
)


def _compose_stop_summary_stats(
    cfg: Any | None,
    last_detail: dict[str, Any] | None,
) -> dict[str, Any]:
    """Итог кооперативной остановки для UI: не пустой last_stats — без сообщения «нет счётчиков»."""
    out: dict[str, Any] = {
        "stopped_by_user": True,
        "note_ru": (
            "Остановка после завершения текущего шага ETL (между запросами к Firebird); "
            "в PostgreSQL зафиксирован последний успешный коммит, курсоры — по etl_state."
        ),
    }
    keys = (
        "phase",
        "cursor_log_id",
        "etl_last_egmid",
        "loaded_rows",
        "parsed_facts",
        "journal_facts",
        "staging_errors",
        "page",
        "pipeline_step",
        "pipeline_steps",
        "messages_batch_rows",
        "journal_batch_rows",
    )
    if last_detail:
        for k in keys:
            if k in last_detail and last_detail[k] is not None:
                out[f"progress_{k}"] = last_detail[k]
    if cfg is None:
        return out
    try:
        from egisz_monitor_corp.pg_warehouse import connect_pg, fetch_etl_watermark_row

        pg = connect_pg(cfg.postgres)
        try:
            row = fetch_etl_watermark_row(pg, cfg.etl.pipeline_name)
            if row:
                out["pg_etl_last_log_id"] = row["last_log_id"]
                out["pg_etl_last_egmid"] = row["last_egmid"]
                out["pg_source_max_egmid"] = row["source_max_egmid"]
                out["pg_messages_snapshot_high_egmid"] = row.get(
                    "messages_snapshot_high_egmid"
                )
        finally:
            pg.close()
    except Exception:  # pragma: no cover - PG при диагностике
        pass
    return out


def _build_sync_failed_progress(
    cfg: Any | None,
    *,
    extra_diag: str | None = None,
) -> dict[str, Any]:
    """Фаза sync_failed + снимок etl_state для UI (асимметрия курсоров при сбое)."""
    progress: dict[str, Any] = {
        "phase": "sync_failed",
        "watermark_note": _SYNC_FAILED_WATERMARK_NOTE_RU,
    }
    if extra_diag:
        progress["diag_error"] = extra_diag[:500]
    if cfg is None:
        return progress
    try:
        from egisz_monitor_corp.pg_warehouse import connect_pg, fetch_etl_watermark_row

        pg = connect_pg(cfg.postgres)
        try:
            row = fetch_etl_watermark_row(pg, cfg.etl.pipeline_name)
            if row:
                progress["cursor_log_id"] = row["last_log_id"]
                progress["etl_last_egmid"] = row["last_egmid"]
                progress["source_max_egmid"] = row["source_max_egmid"]
            else:
                miss = "etl_state: нет строки для пайплайна"
                progress["diag_error"] = (
                    f"{progress['diag_error']} · {miss}" if progress.get("diag_error") else miss
                )
        finally:
            pg.close()
    except Exception as e2:  # pragma: no cover - сеть/PG при диагностике
        tail = str(e2)[:400]
        prev = progress.get("diag_error")
        progress["diag_error"] = f"{prev + ' · ' if prev else ''}PG: {tail}"
    return progress


def _run_sync_job(config_path: str, merged_dict: dict[str, Any] | None = None) -> None:
    def push_progress_event(payload: dict[str, Any]) -> None:
        ph = str((payload or {}).get("phase") or "")
        evt = {
            "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "phase": ph,
            "payload": payload,
        }
        with _state_lock:
            _state["progress"] = payload
            if ph:
                _state["_last_progress_phase"] = ph
            buf = _state.get("progress_events")
            if not isinstance(buf, list):
                buf = []
                _state["progress_events"] = buf
            buf.append(evt)
            # Keep last N events to avoid unbounded growth.
            if len(buf) > 180:
                del buf[: max(0, len(buf) - 180)]

    def log(m: str) -> None:
        with _state_lock:
            _state["message"] = m

    def boot_progress(phase: str, **extra: Any) -> None:
        push_progress_event({"phase": phase, **extra})

    log("Синхронизация: фоновый поток стартовал; загрузка модулей и конфигурации…")
    boot_progress("pipeline_bootstrap")
    cfg: Any = None
    last_detail_snapshot: dict[str, Any] | None = None
    try:
        import os

        os.environ["EGISZ_MONITOR_CONFIG"] = config_path
        from egisz_monitor_corp.config_loader import load_corp_config, parse_corp_config_dict
        from egisz_monitor_corp.etl import run_sync

        def cancel_if_requested() -> None:
            if _cancel_evt.is_set():
                raise EtlCancelledError("Остановка по запросу оператора.")

        if merged_dict is not None:
            cfg = parse_corp_config_dict(merged_dict)
        else:
            cfg = load_corp_config()

        boot_progress("etl_config_ready")

        def on_progress_detail(payload: dict[str, Any]) -> None:
            nonlocal last_detail_snapshot
            last_detail_snapshot = dict(payload)
            push_progress_event(payload)

        stats = run_sync(
            cfg,
            dry_run=False,
            progress_cb=log,
            progress_detail_cb=on_progress_detail,
            cancel_check=cancel_if_requested,
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
            _state["message"] = (
                "Синхронизация успешно завершена: полный проход конвейера "
                "(JPERSONS, EGISZ_LICENSES, EGISZ_MESSAGES, журнал EXCHANGELOG, "
                "исходящие документы и витрина в PostgreSQL)."
            )
    except PipelineLockBusyError as e:  # pragma: no cover - конфликт CronJob ↔ UI
        with _state_lock:
            _state["error"] = str(e)
            _state["last_stats"] = None
            _state["message"] = (
                "Синхронизация не выполнена: параллельный sync уже идёт "
                "(например, CronJob egisz-monitor-sync). Подождите 1–2 минуты и повторите."
            )
            _state["progress"] = _build_sync_failed_progress(cfg)
    except EtlCancelledError:
        summary = _compose_stop_summary_stats(cfg, last_detail_snapshot)
        stop_progress = _build_sync_failed_progress(cfg, extra_diag=None)
        stop_progress["phase"] = "stopped_by_user"
        stop_progress.pop("diag_error", None)
        with _state_lock:
            _state["error"] = None
            _state["last_stats"] = summary
            _state["message"] = (
                "Синхронизация остановлена по запросу. Текущий шаг ETL завершён; "
                "итог зафиксирован (курсоры PostgreSQL — по последнему коммиту, см. счётчики ниже)."
            )
            _state["progress"] = stop_progress
    except Exception as e:  # pragma: no cover
        with _state_lock:
            _state["error"] = str(e)
            _state["last_stats"] = None
            _state["message"] = f"Синхронизация завершилась с ошибкой: {e}"
            _state["progress"] = _build_sync_failed_progress(cfg, extra_diag=str(e))
    finally:
        with _state_lock:
            _state["running"] = False
        _cancel_evt.clear()


def try_request_cancel_sync() -> tuple[bool, str]:
    """Запрос кооперативной остановки UI-синка (флаг проверяется внутри run_sync)."""
    with _state_lock:
        if not _state["running"]:
            return False, "Синхронизация не выполняется."
    _cancel_evt.set()
    return True, (
        "Запрос на остановку принят; ETL завершит текущий шаг и выйдет "
        "(между запросами к Firebird: справочники, страницы журнала ~batch_size строк, чанки MSGID)."
    )


def try_start_sync(
    config_path: str, merged_dict: dict[str, Any] | None = None
) -> tuple[bool, str]:
    with _state_lock:
        if _state["running"]:
            return False, "Синхронизация уже выполняется."
        _state["running"] = True
        _state["sync_attempted"] = True
        _state["error"] = None
        _state["message"] = "Запуск..."
        _state["last_stats"] = None
        _state["progress"] = None
        _state["progress_events"] = []
        _state["_last_progress_phase"] = None
        _cancel_evt.clear()
    # daemon=False: фоновый ETL — долгоживущий поток; при daemon=True он обрывается вместе с процессом
    # при жёстком завершении воркера. Не-daemon даёт шанс завершить поток при обычном shutdown интерпретатора.
    t = threading.Thread(
        target=_run_sync_job, args=(config_path, merged_dict), daemon=False
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
            "progress_events": list(_state["progress_events"])
            if isinstance(_state.get("progress_events"), list)
            else [],
            "sync_attempted": bool(_state["sync_attempted"]),
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

    @app.post("/api/sync/stop")
    def api_sync_stop():  # type: ignore[no-untyped-def]
        ok, msg = try_request_cancel_sync()
        return jsonify({"ok": ok, "message": msg})

    @app.get("/api/sync/status")
    def api_sync_status():  # type: ignore[no-untyped-def]
        return jsonify(get_sync_state())
