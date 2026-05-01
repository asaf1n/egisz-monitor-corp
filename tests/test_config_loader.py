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


def test_logical_config_path_strips_k8s_secret_timestamp_dir(monkeypatch) -> None:
    """EGISZ_MONITOR_CONFIG may contain resolved K8s Secret path; UI shows mount dir + file."""
    monkeypatch.delenv("CONFIG_WRITE_PATH", raising=False)
    monkeypatch.setenv(
        "EGISZ_MONITOR_CONFIG",
        "/app/config/..2026_04_25_03_31_35.1076403807/egisz_monitor.yaml",
    )
    assert logical_config_path().as_posix() == "/app/config/egisz_monitor.yaml"
