"""Firebird charset defaults when loading minimal YAML."""

from __future__ import annotations

from pathlib import Path

from egisz_monitor_corp.config_loader import load_corp_config, logical_config_path, parse_corp_config_dict


def _minimal_yaml_text(charset_line: str | None) -> str:
    fb = """
firebird:
  host: h
  port: 3050
  database: d
  user: u
  password: p"""
    if charset_line is not None:
        fb += f"\n  charset: {charset_line}"
    return (
        fb
        + """
postgres:
  host: ph
  port: 5432
  database: pdb
  user: pu
  password: pp
etl: {}
metabase: {}
"""
    )


def test_firebird_charset_defaults_to_win1251_when_omitted(tmp_path: Path) -> None:
    p = tmp_path / "cfg.yaml"
    p.write_text(_minimal_yaml_text(None), encoding="utf-8")
    cfg = load_corp_config(p)
    assert cfg.firebird.charset == "WIN1251"


def test_firebird_charset_explicit_utf8(tmp_path: Path) -> None:
    p = tmp_path / "cfg.yaml"
    p.write_text(_minimal_yaml_text("UTF8"), encoding="utf-8")
    cfg = load_corp_config(p)
    assert cfg.firebird.charset == "UTF8"


def test_etl_max_msgtext_bytes_from_yaml(tmp_path: Path) -> None:
    p = tmp_path / "cfg.yaml"
    p.write_text(
        _minimal_yaml_text(None).replace(
            "etl: {}",
            "etl:\n  max_msgtext_bytes: 2048\n",
        ),
        encoding="utf-8",
    )
    cfg = load_corp_config(p)
    assert cfg.etl.max_msgtext_bytes == 2048


def test_etl_firebird_query_timeout_defaults_and_clamp() -> None:
    cfg = parse_corp_config_dict(
        {
            "firebird": {"host": "h", "port": 1, "database": "d", "user": "u", "password": "p"},
            "postgres": {"host": "h", "port": 2, "database": "d", "user": "u", "password": "p"},
            "etl": {},
        },
        use_yaml_postgres_only=True,
    )
    assert cfg.etl.firebird_query_timeout_sec == 900
    assert cfg.etl.skip_firebird_progress_count is False
    assert cfg.etl.facts_upsert_chunk_size == 500
    assert cfg.etl.batch_size == 8000
    assert cfg.etl.pg_upsert_statement_timeout_sec == 120
    hi = parse_corp_config_dict(
        {
            "firebird": {"host": "h", "port": 1, "database": "d", "user": "u", "password": "p"},
            "postgres": {"host": "h", "port": 2, "database": "d", "user": "u", "password": "p"},
            "etl": {"firebird_query_timeout_sec": 99999, "skip_firebird_progress_count": True},
        },
        use_yaml_postgres_only=True,
    )
    assert hi.etl.firebird_query_timeout_sec == 7200
    assert hi.etl.skip_firebird_progress_count is True


def test_etl_facts_upsert_and_pg_timeout_from_yaml() -> None:
    lo = parse_corp_config_dict(
        {
            "firebird": {"host": "h", "port": 1, "database": "d", "user": "u", "password": "p"},
            "postgres": {"host": "h", "port": 2, "database": "d", "user": "u", "password": "p"},
            "etl": {"facts_upsert_chunk_size": 200, "pg_upsert_statement_timeout_sec": None},
        },
        use_yaml_postgres_only=True,
    )
    assert lo.etl.facts_upsert_chunk_size == 200
    assert lo.etl.pg_upsert_statement_timeout_sec is None


def test_etl_max_msgtext_bytes_zero_means_disabled() -> None:
    cfg = parse_corp_config_dict(
        {
            "firebird": {"host": "h", "port": 1, "database": "d", "user": "u", "password": "p"},
            "postgres": {"host": "h", "port": 2, "database": "d", "user": "u", "password": "p"},
            "etl": {"max_msgtext_bytes": 0},
        },
        use_yaml_postgres_only=True,
    )
    assert cfg.etl.max_msgtext_bytes is None


def test_etl_sync_window_days_zero_from_yaml(tmp_path: Path) -> None:
    cfg = parse_corp_config_dict(
        {
            "firebird": {"host": "h", "port": 3050, "database": "d", "user": "u", "password": "p"},
            "postgres": {"host": "h", "port": 5432, "database": "d", "user": "u", "password": "p"},
            "etl": {"sync_window_days": 0},
            "metabase": {},
            "auto_sync": {},
        }
    )
    assert cfg.etl.sync_window_days == 0


def test_auto_sync_and_etl_batch_from_minimal_yaml(tmp_path: Path) -> None:
    p = tmp_path / "cfg.yaml"
    p.write_text(
        _minimal_yaml_text(None).replace("etl: {}", "etl:\n  batch_size: 400\n"),
        encoding="utf-8",
    )
    cfg = load_corp_config(p)
    assert cfg.auto_sync.enabled is False
    assert cfg.auto_sync.schedule_cron == "*/15 * * * *"
    assert cfg.auto_sync.timezone == "Etc/UTC"
    assert cfg.etl.batch_size == 400


def test_auto_sync_from_yaml_and_batch_clamp() -> None:
    cfg = parse_corp_config_dict(
        {
            "firebird": {"host": "h", "port": 1, "database": "d", "user": "u", "password": "p"},
            "postgres": {"host": "h", "port": 2, "database": "d", "user": "u", "password": "p"},
            "etl": {"batch_size": 20},
            "auto_sync": {"enabled": True, "schedule_cron": "0 * * * *", "timezone": "Europe/Moscow"},
        },
        use_yaml_postgres_only=True,
    )
    assert cfg.etl.batch_size == 20
    assert cfg.auto_sync.enabled is True
    assert cfg.auto_sync.schedule_cron == "0 * * * *"
    assert cfg.auto_sync.timezone == "Europe/Moscow"


def test_logical_config_path_strips_k8s_secret_timestamp_dir(monkeypatch) -> None:
    """EGISZ_MONITOR_CONFIG may contain resolved K8s Secret path; UI shows mount dir + file."""
    monkeypatch.delenv("CONFIG_WRITE_PATH", raising=False)
    monkeypatch.setenv(
        "EGISZ_MONITOR_CONFIG",
        "/app/config/..2026_04_25_03_31_35.1076403807/egisz_monitor.yaml",
    )
    assert logical_config_path().as_posix() == "/app/config/egisz_monitor.yaml"
