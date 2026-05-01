"""PostgreSQL logical backup/restore via standard pg_dump/pg_restore CLI (Bookworm client in conf-ui image)."""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
from pathlib import Path

from egisz_monitor_corp.config_loader import PostgresConfig


def _require_cli(name: str) -> str:
    p = shutil.which(name)
    if not p:
        raise RuntimeError(f"{name} not found in PATH (install postgresql-client in the image).")
    return p


def pg_dump_custom_bytes(cfg: PostgresConfig, *, timeout_sec: int = 900) -> bytes:
    """Full custom-format dump (-Fc) to stdout. Uses PGPASSWORD; no host secrets in argv beyond user/host/db."""
    _require_cli("pg_dump")
    env = os.environ.copy()
    env["PGPASSWORD"] = cfg.password
    cmd = [
        "pg_dump",
        "-h",
        cfg.host,
        "-p",
        str(int(cfg.port)),
        "-U",
        cfg.user,
        "-d",
        cfg.database,
        "-Fc",
        "--no-owner",
    ]
    proc = subprocess.run(
        cmd,
        env=env,
        capture_output=True,
        timeout=timeout_sec,
        check=False,
    )
    if proc.returncode != 0:
        err = (proc.stderr or b"").decode("utf-8", errors="replace")[:4000]
        raise RuntimeError(f"pg_dump failed (exit {proc.returncode}): {err}")
    return proc.stdout or b""


def pg_restore_data_only(cfg: PostgresConfig, dump_path: Path, *, timeout_sec: int = 3600) -> str:
    """Fixed policy: data-only restore (schema must already match). Returns stderr tail for UI."""
    _require_cli("pg_restore")
    env = os.environ.copy()
    env["PGPASSWORD"] = cfg.password
    cmd = [
        "pg_restore",
        "-h",
        cfg.host,
        "-p",
        str(int(cfg.port)),
        "-U",
        cfg.user,
        "-d",
        cfg.database,
        "--no-owner",
        "--no-acl",
        "--data-only",
        "--verbose",
        str(dump_path),
    ]
    proc = subprocess.run(
        cmd,
        env=env,
        capture_output=True,
        timeout=timeout_sec,
        check=False,
    )
    out = (proc.stdout or b"").decode("utf-8", errors="replace")
    err = (proc.stderr or b"").decode("utf-8", errors="replace")
    # pg_restore uses exit 1 for warnings; >1 is hard failure
    if proc.returncode > 1:
        raise RuntimeError(f"pg_restore failed (exit {proc.returncode}): {(err or out)[:8000]}")
    return (err or out)[-12000:] if (err or out) else "pg_restore finished (no stderr)."


def restore_upload_to_temp_and_run(cfg: PostgresConfig, data: bytes, *, timeout_sec: int = 3600) -> str:
    """Write upload bytes to a temp file and run pg_restore_data_only."""
    if not data:
        raise ValueError("empty dump file")
    with tempfile.NamedTemporaryFile(suffix=".dump", prefix="egisz_restore_", delete=False) as tf:
        tf.write(data)
        path = Path(tf.name)
    try:
        return pg_restore_data_only(cfg, path, timeout_sec=timeout_sec)
    finally:
        path.unlink(missing_ok=True)
