"""Load YAML configuration for Firebird, PostgreSQL, and ETL options."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

# Kubernetes Secret volume: real path contains a rotated directory like ..2026_04_25_03_31_35.*
_K8S_SECRET_TS_DIR = re.compile(r"^\.\.\d{4}_\d{2}_\d{2}_")


def _strip_k8s_secret_timestamp_dir(p: Path) -> Path:
    """Collapse ..YYYY_MM_DD_* path segment to parent + basename (stable mount path for UI)."""
    parts = p.parts
    for i, part in enumerate(parts):
        if _K8S_SECRET_TS_DIR.match(part):
            prefix = Path(*parts[:i]) if i else Path(".")
            return prefix / parts[-1]
    return p

try:
    import yaml
except ImportError as e:  # pragma: no cover
    raise ImportError("PyYAML is required. Install egisz-monitor-corp with dependencies.") from e


def logical_config_path() -> Path:
    """Path for UI and EGISZ_MONITOR_CONFIG: does not resolve symlinks (K8s Secret mounts use ..date.. dirs)."""
    w = os.environ.get("CONFIG_WRITE_PATH")
    if w:
        return _strip_k8s_secret_timestamp_dir(Path(w).expanduser())
    env = os.environ.get("EGISZ_MONITOR_CONFIG")
    if env:
        return _strip_k8s_secret_timestamp_dir(Path(env).expanduser())
    root = Path(__file__).resolve().parents[1]
    return root / "config" / "egisz_monitor.yaml"


def default_config_path() -> Path:
    env = os.environ.get("EGISZ_MONITOR_CONFIG")
    if env:
        return Path(env).expanduser().resolve()
    root = Path(__file__).resolve().parents[1]
    return (root / "config" / "egisz_monitor.yaml").resolve()


@dataclass
class FirebirdConfig:
    host: str
    port: int
    database: str
    user: str
    password: str
    # Infoclinica / WIN1251 — типичный случай; для БД в UTF8 задайте в YAML firebird.charset: UTF8.
    charset: str = "WIN1251"
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
    # None / не задано — без лимита. UTF-8 размер MSGTEXT; при превышении строка журнала пропускается (staging MSGTEXT_TOO_LARGE).
    max_msgtext_bytes: int | None = None
    # Таймаут каждого SELECT к Firebird в ETL (ThreadPoolExecutor). COUNT по большой EGISZ_MESSAGES часто > 300 с.
    firebird_query_timeout_sec: int = 900
    # Не выполнять COUNT(*) для прогресс-бара (журнал/сообщения) — мгновенно, без процента в UI.
    skip_firebird_progress_count: bool = False


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
            f"Config file not found: {cfg_path}. Copy config/egisz_monitor.example.yaml "
            f"to config/egisz_monitor.yaml or set EGISZ_MONITOR_CONFIG."
        )
    raw = yaml.safe_load(cfg_path.read_text(encoding="utf-8"))
    return parse_corp_config_dict(raw)


def parse_corp_config_dict(
    raw: dict[str, Any],
    *,
    use_yaml_postgres_only: bool = False,
) -> CorpAppConfig:
    """Build config from a YAML root mapping (used by load_corp_config and Config UI form preview/tests)."""
    if not isinstance(raw, dict):
        raise ValueError("Config root must be a mapping")

    fb = raw.get("firebird") or {}
    pg = raw.get("postgres") or {}
    etl = raw.get("etl") or {}
    mb = raw.get("metabase") or {}
    if not isinstance(fb, dict) or not isinstance(pg, dict) or not isinstance(etl, dict):
        raise ValueError("firebird, postgres, and etl must be mappings")

    _raw_mmb = etl.get("max_msgtext_bytes")
    if _raw_mmb is None or _raw_mmb == "":
        max_msgtext_bytes: int | None = None
    else:
        max_msgtext_bytes = _int(_raw_mmb)
        if max_msgtext_bytes <= 0:
            max_msgtext_bytes = None
    if use_yaml_postgres_only:
        pg_host = _str(pg.get("host"))
        pg_port = _int(pg.get("port"))
        pg_db = _str(pg.get("database"))
        pg_user = _str(pg.get("user"))
        pg_password = _str(pg.get("password"))
        pg_schema = _str(pg.get("schema"), "public")
    else:
        pg_host = _env_nonempty("EGISZ_MONITOR_POSTGRES_HOST") or _str(pg.get("host"))
        pg_port = _int(_env_nonempty("EGISZ_MONITOR_POSTGRES_PORT") or pg.get("port"))
        pg_db = _env_nonempty("EGISZ_MONITOR_POSTGRES_DB") or _str(pg.get("database"))
        pg_user = _env_nonempty("EGISZ_MONITOR_POSTGRES_USER") or _str(pg.get("user"))
        pg_password = _env_nonempty("EGISZ_MONITOR_POSTGRES_PASSWORD") or _str(pg.get("password"))
        pg_schema = _env_nonempty("EGISZ_MONITOR_POSTGRES_SCHEMA") or _str(pg.get("schema"), "public")

    _fb_to = _int(etl.get("firebird_query_timeout_sec"), 900)
    _fb_to = max(30, min(_fb_to, 7200))

    return CorpAppConfig(
        firebird=FirebirdConfig(
            host=_str(fb.get("host")),
            port=_int(fb.get("port")),
            database=_str(fb.get("database")),
            user=_str(fb.get("user")),
            password=_str(fb.get("password")),
            charset=_str(fb.get("charset"), "WIN1251"),
            page_size=_int(fb.get("page_size"), 4096),
        ),
        postgres=PostgresConfig(
            host=pg_host,
            port=pg_port,
            database=pg_db,
            user=pg_user,
            password=pg_password,
            schema=pg_schema,
        ),
        etl=EtlConfig(
            batch_size=_int(etl.get("batch_size"), 500),
            pipeline_name=_str(etl.get("pipeline_name"), "firebird_exchangelog"),
            sync_window_days=_int(etl.get("sync_window_days"), 30),
            full_scan=_bool(etl.get("full_scan"), False),
            source_query=_str(etl.get("source_query"), "") or None,
            max_msgtext_bytes=max_msgtext_bytes,
            firebird_query_timeout_sec=_fb_to,
            skip_firebird_progress_count=_bool(etl.get("skip_firebird_progress_count"), False),
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
