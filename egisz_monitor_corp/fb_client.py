"""Firebird read-only access via firebird-driver (requires fbclient on PATH / FB_CLIENT_LIBRARY)."""

from __future__ import annotations

from collections.abc import Iterator, Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeoutError
from typing import Any

from egisz_monitor_corp.config_loader import FirebirdConfig


def firebird_dsn(cfg: FirebirdConfig) -> str:
    """Remote DSN: host/port:database — database is path or alias as on the Firebird server (not a local pod path)."""
    return f"{cfg.host}/{int(cfg.port)}:{cfg.database}"


def connect_firebird(cfg: FirebirdConfig):  # type: ignore[no-untyped-def]
    """charset в конфиге должен совпадать с фактической кодировкой строк в БД (JPERSONS.JNAME и т.д.).

    Таймаут установления TCP/чтения для `firebird.driver.connect` в публичном API не задаётся
    (см. сигнатуру connect: только database, user, password, charset, …). Ограничение долгих
    запросов — на стороне сервера Firebird (firebird.conf) или прерывание на уровне приложения
    (например ThreadPoolExecutor + timeout в вызывающем коде, как в healthcheck UI).
    """
    from firebird.driver import connect

    return connect(
        firebird_dsn(cfg),
        user=cfg.user,
        password=cfg.password,
        charset=cfg.charset or None,
    )


def _fmt_fb_timestamp(v: Any) -> str:
    """Привести дату/время из Firebird к строке для JSON/UI."""
    if v is None:
        return ""
    iso = getattr(v, "isoformat", None)
    if callable(iso):
        return iso()
    return str(v)


def fetch_firebird_source_peaks(cfg: FirebirdConfig) -> dict[str, Any]:
    """
    Снимок «верхушек» справочника в Firebird: последний EGMID в EGISZ_MESSAGES,
    последний MODIFYDATE в EGISZ_LICENSES (для конфиг-UI и диагностики).

    Один SELECT и одно соединение — меньше задержка, чем два последовательных MAX().
    """
    out: dict[str, Any] = {"max_egmid": None, "max_licenses_modifydate": None, "error": None}
    sql = """
SELECT
    (SELECT MAX(m.EGMID) FROM EGISZ_MESSAGES m) AS max_egmid,
    (SELECT MAX(l.MODIFYDATE) FROM EGISZ_LICENSES l) AS max_licenses_modifydate
FROM RDB$DATABASE
""".strip()
    try:
        rows = fetch_all(cfg, sql, timeout_sec=60)  # 60s for peaks check
        if rows:
            row = rows[0]
            v = row.get("max_egmid")
            if v is not None:
                try:
                    out["max_egmid"] = int(v)
                except (TypeError, ValueError):
                    out["max_egmid"] = v
            v2 = row.get("max_licenses_modifydate")
            if v2 is not None:
                out["max_licenses_modifydate"] = _fmt_fb_timestamp(v2)
    except Exception as e:  # pragma: no cover - network / driver
        out["error"] = str(e)
    return out


def _fetch_all_impl(cfg: FirebirdConfig, sql: str, params: Sequence[Any] | Mapping[str, Any] | None) -> list[dict[str, Any]]:
    """Inner implementation: connect, execute, fetch."""
    con = connect_firebird(cfg)
    try:
        cur = con.cursor()
        cur.execute(sql, params or ())
        desc = cur.description
        names = [d[0].lower() if d and d[0] else f"col{i}" for i, d in enumerate(desc or [])]
        rows = cur.fetchall()
        return [dict(zip(names, r)) for r in rows]
    finally:
        con.close()


def fetch_all(
    cfg: FirebirdConfig,
    sql: str,
    params: Sequence[Any] | Mapping[str, Any] | None = None,
    timeout_sec: int = 300,
) -> list[dict[str, Any]]:
    """Run SELECT with timeout protection. Default 5 min (300s) for ad-hoc callers; ETL uses etl.firebird_query_timeout_sec via _etl_fb_fetch.

    Таймаут оборачивает весь запрос (connect + execute + fetchall) в ThreadPoolExecutor и прерывается
    если Firebird зависает. Исключение TimeoutError переводится в RuntimeError для совместимости.
    """
    with ThreadPoolExecutor(max_workers=1) as executor:
        try:
            future = executor.submit(_fetch_all_impl, cfg, sql, params)
            return future.result(timeout=timeout_sec)
        except FutureTimeoutError as e:
            raise RuntimeError(
                f"Firebird query timeout after {timeout_sec}s (SQL length {len(sql)} chars). "
                "For ETL: raise etl.firebird_query_timeout_sec in YAML; check indexes and sync_window_days."
            ) from e


def iter_batches(
    cfg: FirebirdConfig, sql: str, params: Sequence[Any] | Mapping[str, Any] | None = None, arraysize: int = 500
) -> Iterator[list[dict[str, Any]]]:
    """Iterate in batches (arraysize rows per fetch). No per-batch timeout — caller must manage total time."""
    con = connect_firebird(cfg)
    try:
        cur = con.cursor()
        cur.execute(sql, params or ())
        desc = cur.description
        names = [d[0].lower() if d and d[0] else f"col{i}" for i, d in enumerate(desc or [])]
        while True:
            chunk = cur.fetchmany(arraysize)
            if not chunk:
                break
            yield [dict(zip(names, r)) for r in chunk]
    finally:
        con.close()
