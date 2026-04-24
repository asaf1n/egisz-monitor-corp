"""Firebird read-only access via firebird-driver (requires fbclient on PATH / FB_CLIENT_LIBRARY)."""

from __future__ import annotations

from collections.abc import Iterator, Mapping, Sequence
from typing import Any

from egisz_monitor_corp.config_loader import FirebirdConfig


def firebird_dsn(cfg: FirebirdConfig) -> str:
    """Remote DSN: host/port:database (database is path or alias on server)."""
    return f"{cfg.host}/{int(cfg.port)}:{cfg.database}"


def connect_firebird(cfg: FirebirdConfig):  # type: ignore[no-untyped-def]
    from firebird.driver import connect

    return connect(
        firebird_dsn(cfg),
        user=cfg.user,
        password=cfg.password,
        charset=cfg.charset or None,
    )


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
