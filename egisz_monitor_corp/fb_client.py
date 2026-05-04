"""Firebird read-only access via firebird-driver (requires fbclient on PATH / FB_CLIENT_LIBRARY)."""

from __future__ import annotations

from collections.abc import Callable, Iterator, Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeoutError
from typing import Any

from egisz_monitor_corp.config_loader import FirebirdConfig


def firebird_dsn(cfg: FirebirdConfig) -> str:
    """Remote DSN: host/port:database — database is path or alias as on the Firebird server (not a local pod path)."""
    return f"{cfg.host}/{int(cfg.port)}:{cfg.database}"


def connect_firebird(cfg: FirebirdConfig):  # type: ignore[no-untyped-def]
    """charset в конфиге должен совпадать с фактической кодировкой строк в БД.

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


def fetch_firebird_max_license_modifydate(cfg: FirebirdConfig) -> dict[str, Any]:
    """MAX(MODIFYDATE) по EGISZ_LICENSES в Firebird — для сравнения с кешем в etl_state (healthcheck).

    Значение last_egmid для UI — из PostgreSQL (etl_state и healthcheck), без MAX(EGMID) по сообщениям в Firebird.
    """
    out: dict[str, Any] = {"max_licenses_modifydate": None, "error": None}
    sql = """
SELECT MAX(l.MODIFYDATE) AS max_licenses_modifydate
FROM EGISZ_LICENSES l
""".strip()
    try:
        rows = fetch_all(cfg, sql, timeout_sec=60)
        if rows:
            row = rows[0]
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
    *,
    wait_tick_sec: int | None = None,
    on_wait_tick: Callable[[int], None] | None = None,
) -> list[dict[str, Any]]:
    """Run SELECT with timeout protection. Default 5 min (300s) for ad-hoc callers; ETL uses etl.firebird_query_timeout_sec via _etl_fb_fetch.

    Таймаут оборачивает весь запрос (connect + execute + fetchall) в ThreadPoolExecutor и прерывается
    если Firebird зависает. Исключение TimeoutError переводится в RuntimeError для совместимости.

    Если заданы оба ``wait_tick_sec`` и ``on_wait_tick``, ожидание ``future.result`` режется на тики:
    UI/ETL получает секунды накопленного ожидания без завершения запроса (долгий первый SELECT).
    """
    err_tail = (
        f"Firebird query timeout after {timeout_sec}s (SQL length {len(sql)} chars). "
        "For ETL: raise etl.firebird_query_timeout_sec in YAML; check indexes and EGMID/LOGID cursors."
    )

    def _raise_timeout(from_exc: BaseException | None = None) -> None:
        raise RuntimeError(err_tail) from from_exc

    use_ticks = wait_tick_sec is not None and on_wait_tick is not None
    if not use_ticks:
        with ThreadPoolExecutor(max_workers=1) as executor:
            try:
                future = executor.submit(_fetch_all_impl, cfg, sql, params)
                return future.result(timeout=timeout_sec)
            except FutureTimeoutError as e:
                _raise_timeout(e)

    tick = max(1, min(int(wait_tick_sec or 1), max(1, int(timeout_sec))))
    elapsed = 0
    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(_fetch_all_impl, cfg, sql, params)
        while True:
            slice_sec = min(tick, max(0, timeout_sec - elapsed))
            if slice_sec <= 0:
                _raise_timeout()
            try:
                return future.result(timeout=slice_sec)
            except FutureTimeoutError:
                elapsed += slice_sec
                if elapsed >= timeout_sec:
                    _raise_timeout()
                on_wait_tick(elapsed)


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
