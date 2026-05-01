"""Unit tests for pg_dump/pg_restore helpers (mocked subprocess)."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from egisz_monitor_corp.config_loader import PostgresConfig
from egisz_monitor_corp import pg_cli_backup


@pytest.fixture()
def pg_cfg() -> PostgresConfig:
    return PostgresConfig(host="h", port=5432, database="db1", user="u", password="secret", schema="public")


def test_pg_dump_custom_bytes_success(pg_cfg: PostgresConfig, monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, object] = {}

    def fake_run(cmd: list[str], env: dict[str, str], capture_output: bool, timeout: int, check: bool):
        captured["cmd"] = cmd
        captured["env_pw"] = env.get("PGPASSWORD")
        return SimpleNamespace(returncode=0, stdout=b"CUSTOM", stderr=b"")

    monkeypatch.setattr(pg_cli_backup.shutil, "which", lambda _: "/bin/pg_dump")
    monkeypatch.setattr(pg_cli_backup.subprocess, "run", fake_run)
    out = pg_cli_backup.pg_dump_custom_bytes(pg_cfg)
    assert out == b"CUSTOM"
    assert captured["env_pw"] == "secret"
    cmd = captured["cmd"]
    assert isinstance(cmd, list)
    assert "secret" not in " ".join(cmd)


def test_pg_dump_custom_bytes_failure(pg_cfg: PostgresConfig, monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_run(cmd: list[str], env: dict[str, str], capture_output: bool, timeout: int, check: bool):
        return SimpleNamespace(returncode=1, stdout=b"", stderr=b"connection refused")

    monkeypatch.setattr(pg_cli_backup.shutil, "which", lambda _: "/bin/pg_dump")
    monkeypatch.setattr(pg_cli_backup.subprocess, "run", fake_run)
    with pytest.raises(RuntimeError, match="pg_dump"):
        pg_cli_backup.pg_dump_custom_bytes(pg_cfg)


def test_pg_restore_data_only_warning_exit_ok(
    pg_cfg: PostgresConfig, monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    dump = tmp_path / "x.dump"
    dump.write_bytes(b"x")

    def fake_run(cmd: list[str], env: dict[str, str], capture_output: bool, timeout: int, check: bool):
        return SimpleNamespace(returncode=1, stdout=b"", stderr=b"NOTICE: restore warning\n")

    monkeypatch.setattr(pg_cli_backup.shutil, "which", lambda _: "/bin/pg_restore")
    monkeypatch.setattr(pg_cli_backup.subprocess, "run", fake_run)
    msg = pg_cli_backup.pg_restore_data_only(pg_cfg, dump)
    assert "NOTICE" in msg


def test_pg_restore_data_only_hard_failure(
    pg_cfg: PostgresConfig, monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    dump = tmp_path / "x.dump"
    dump.write_bytes(b"x")

    def fake_run(cmd: list[str], env: dict[str, str], capture_output: bool, timeout: int, check: bool):
        return SimpleNamespace(returncode=2, stdout=b"", stderr=b"fatal")

    monkeypatch.setattr(pg_cli_backup.shutil, "which", lambda _: "/bin/pg_restore")
    monkeypatch.setattr(pg_cli_backup.subprocess, "run", fake_run)
    with pytest.raises(RuntimeError, match="pg_restore"):
        pg_cli_backup.pg_restore_data_only(pg_cfg, dump)


def test_restore_upload_to_temp_and_run_deletes_temp(
    pg_cfg: PostgresConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: dict[str, object] = {}

    def fake_restore(cfg: PostgresConfig, path, *, timeout_sec: int = 3600):
        seen["path"] = path
        seen["exists_mid"] = path.is_file()
        return "done"

    monkeypatch.setattr(pg_cli_backup, "pg_restore_data_only", fake_restore)
    monkeypatch.setattr(pg_cli_backup.shutil, "which", lambda _: "/bin/pg_restore")
    assert pg_cli_backup.restore_upload_to_temp_and_run(pg_cfg, b"abc") == "done"
    assert seen["exists_mid"] is True
    p = seen["path"]
    assert isinstance(p, Path)
    assert not p.is_file()


def test_restore_upload_empty_raises(pg_cfg: PostgresConfig, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(pg_cli_backup.shutil, "which", lambda _: "/bin/pg_restore")
    with pytest.raises(ValueError, match="empty"):
        pg_cli_backup.restore_upload_to_temp_and_run(pg_cfg, b"")
