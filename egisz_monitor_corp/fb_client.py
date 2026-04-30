"""Firebird read-only access via firebird-driver (requires fbclient on PATH / FB_CLIENT_LIBRARY)."""

from __future__ import annotations

from collections.abc import Iterator, Mapping, Sequence
from typing import Any

from egisz_monitor_corp.config_loader import FirebirdConfig


def firebird_dsn(cfg: FirebirdConfig) -> str:
    """Remote DSN: host/port:database — database is path or alias as on the Firebird server (not a local pod path)."""
    return f"{cfg.host}/{int(cfg.port)}:{cfg.database}"


def connect_firebird(cfg: FirebirdConfig):  # type: ignore[no-untyped-def]
    """charset в конфиге должен совпадать с фактической кодировкой строк в БД (JPERSONS.JNAME и т.д.)."""
    from firebird.driver import connect

    return connect(
        firebird_dsn(cfg),
        user=cfg.user,
        password=cfg.password,
        charset=cfg.charset or None,
    )


def fetch_firebird_source_peaks(cfg: FirebirdConfig) -> dict[str, Any]:
    """
    Снимок «верхушек» справочника в Firebird: последний EGMID в EGISZ_MESSAGES,
    последний MODIFYDATE в EGISZ_LICENSES (для конфиг-UI и диагностики).
    """
    out: dict[str, Any] = {"max_egmid": None, "max_licenses_modifydate": None, "error": None}
    try:
        r1 = fetch_all(cfg, "SELECT MAX(m.EGMID) AS max_egmid FROM EGISZ_MESSAGES m")
        r2 = fetch_all(cfg, "SELECT MAX(l.MODIFYDATE) AS max_licenses_modifydate FROM EGISZ_LICENSES l")
        if r1:
            v = r1[0].get("max_egmid")
            if v is not None:
                try:
                    out["max_egmid"] = int(v)
                except (TypeError, ValueError):
                    out["max_egmid"] = v
        if r2:
            v2 = r2[0].get("max_licenses_modifydate")
            if v2 is not None:
                iso = getattr(v2, "isoformat", None)
                out["max_licenses_modifydate"] = iso() if callable(iso) else str(v2)
    except Exception as e:  # pragma: no cover - network / driver
        out["error"] = str(e)
    return out


def fetch_all(cfg: FirebirdConfig, sql: str, params: Sequence[Any] | Mapping[str, Any] | None = None) -> list[dict[str, Any]]:
    """Run SELECT and return list of row dicts (lowercase keys)."""
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


def iter_batches(
    cfg: FirebirdConfig, sql: str, params: Sequence[Any] | Mapping[str, Any] | None = None, arraysize: int = 500
) -> Iterator[list[dict[str, Any]]]:
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
