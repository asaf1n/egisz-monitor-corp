"""Load YAML configuration for Firebird, PostgreSQL, and ETL options."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

try:
    import yaml
except ImportError as e:  # pragma: no cover
    raise ImportError("PyYAML is required. Install egisz-monitor-corp with dependencies.") from e


def default_config_path() -> Path:
    env = os.environ.get("EGISZ_CORP_CONFIG")
    if env:
        return Path(env).expanduser().resolve()
    root = Path(__file__).resolve().parents[1]
    return (root / "config" / "egisz_corp.yaml").resolve()


@dataclass
class FirebirdConfig:
    host: str
    port: int
    database: str
    user: str
    password: str
    charset: str = "UTF8"
    page_size: int = 4096


@dataclass
class PostgresConfig:
    host: str
    port: int
    database: str
    user: str
    password: str
    schema: str = "public"


@dataclass
class EtlConfig:
    batch_size: int = 500
    pipeline_name: str = "firebird_exchangelog"
    sync_window_days: int = 30
    full_scan: bool = False
    source_query: str | None = None


@dataclass
class CorpAppConfig:
    firebird: FirebirdConfig
    postgres: PostgresConfig
    etl: EtlConfig
    metabase: dict[str, Any]


def _str(v: Any, default: str | None = None) -> str:
    if v is None:
        if default is not None:
            return default
        raise ValueError("missing string")
    s = str(v).strip()
    if not s and default is not None:
        return default
    if not s:
        raise ValueError("empty string")
    return s


def _int(v: Any, default: int | None = None) -> int:
    if v is None and default is not None:
        return default
    if isinstance(v, bool):
        raise ValueError("invalid int")
    return int(v)


def _bool(v: Any, default: bool = False) -> bool:
    if v is None:
        return default
    return bool(v)


def _env_nonempty(key: str) -> str | None:
    """Return stripped env value, or None if unset/blank (do not treat as override)."""
    v = os.environ.get(key)
    if v is None:
        return None
    s = str(v).strip()
    return s if s else None


def load_corp_config(path: Path | None = None) -> CorpAppConfig:
    cfg_path = path or default_config_path()
    if not cfg_path.is_file():
        raise FileNotFoundError(
            f"Config file not found: {cfg_path}. Copy config/egisz_corp.example.yaml "
            f"to config/egisz_corp.yaml or set EGISZ_CORP_CONFIG."
        )
    raw = yaml.safe_load(cfg_path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError("Config root must be a mapping")

    fb = raw.get("firebird") or {}
    pg = raw.get("postgres") or {}
    etl = raw.get("etl") or {}
    mb = raw.get("metabase") or {}
    if not isinstance(fb, dict) or not isinstance(pg, dict) or not isinstance(etl, dict):
        raise ValueError("firebird, postgres, and etl must be mappings")

    return CorpAppConfig(
        firebird=FirebirdConfig(
            host=_str(fb.get("host")),
            port=_int(fb.get("port")),
            database=_str(fb.get("database")),
            user=_str(fb.get("user")),
            password=_str(fb.get("password")),
            charset=_str(fb.get("charset"), "UTF8"),
            page_size=_int(fb.get("page_size"), 4096),
        ),
        postgres=PostgresConfig(
            host=_env_nonempty("EGISZ_CORP_POSTGRES_HOST") or _str(pg.get("host")),
            port=_int(_env_nonempty("EGISZ_CORP_POSTGRES_PORT") or pg.get("port")),
            database=_env_nonempty("EGISZ_CORP_POSTGRES_DB") or _str(pg.get("database")),
            user=_env_nonempty("EGISZ_CORP_POSTGRES_USER") or _str(pg.get("user")),
            password=_env_nonempty("EGISZ_CORP_POSTGRES_PASSWORD") or _str(pg.get("password")),
            schema=_env_nonempty("EGISZ_CORP_POSTGRES_SCHEMA") or _str(pg.get("schema"), "public"),
        ),
        etl=EtlConfig(
            batch_size=_int(etl.get("batch_size"), 500),
            pipeline_name=_str(etl.get("pipeline_name"), "firebird_exchangelog"),
            sync_window_days=_int(etl.get("sync_window_days"), 30),
            full_scan=_bool(etl.get("full_scan"), False),
            source_query=_str(etl.get("source_query"), "") or None,
        ),
        metabase=dict(mb) if isinstance(mb, dict) else {},
    )


def save_corp_config(data: dict[str, Any], path: Path | None = None) -> None:
    """Write YAML atomically (used by Flask config UI)."""
    cfg_path = path or default_config_path()
    cfg_path.parent.mkdir(parents=True, exist_ok=True)
    text = yaml.safe_dump(data, allow_unicode=True, default_flow_style=False, sort_keys=False)
    tmp = cfg_path.with_suffix(cfg_path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(cfg_path)
