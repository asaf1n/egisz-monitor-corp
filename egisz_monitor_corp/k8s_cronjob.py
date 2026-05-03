"""Синхронизация CronJob `egisz-monitor-sync` с блоком `auto_sync` в YAML (suspend / schedule / timeZone)."""

from __future__ import annotations

import json
import os
import shutil
import ssl
import subprocess
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Mapping

DEFAULT_NAMESPACE = "egisz-monitor"
DEFAULT_CRONJOB_NAME = "egisz-monitor-sync"


def _read_in_cluster_namespace() -> str:
    p = Path("/var/run/secrets/kubernetes.io/serviceaccount/namespace")
    if p.is_file():
        s = p.read_text(encoding="utf-8").strip()
        if s:
            return s
    return DEFAULT_NAMESPACE


def _in_cluster() -> bool:
    return Path("/var/run/secrets/kubernetes.io/serviceaccount/token").is_file()


def build_cronjob_merge_patch(auto_sync: Mapping[str, Any] | None) -> dict[str, Any]:
    """Merge-patch тела для batch/v1 CronJob: suspend = not enabled."""
    raw = dict(auto_sync or {})
    enabled = bool(raw.get("enabled"))
    schedule = str(raw.get("schedule_cron") or "*/15 * * * *").strip() or "*/15 * * * *"
    tz = str(raw.get("timezone") or "Etc/UTC").strip() or "Etc/UTC"
    return {"spec": {"suspend": not enabled, "schedule": schedule, "timeZone": tz}}


def patch_cronjob_in_cluster(namespace: str, name: str, merge_patch: dict[str, Any]) -> None:
    token_path = "/var/run/secrets/kubernetes.io/serviceaccount/token"
    ca_path = "/var/run/secrets/kubernetes.io/serviceaccount/ca.crt"
    token = Path(token_path).read_text(encoding="utf-8").strip()
    host = os.environ.get("KUBERNETES_SERVICE_HOST", "").strip()
    port = os.environ.get("KUBERNETES_SERVICE_PORT", "443").strip() or "443"
    if not host:
        raise RuntimeError("KUBERNETES_SERVICE_HOST не задан")

    ns_q = urllib.parse.quote(namespace, safe="")
    name_q = urllib.parse.quote(name, safe="")
    url = f"https://{host}:{port}/apis/batch/v1/namespaces/{ns_q}/cronjobs/{name_q}"
    body = json.dumps(merge_patch, separators=(",", ":")).encode("utf-8")
    ctx = ssl.create_default_context(cafile=ca_path)
    req = urllib.request.Request(
        url,
        data=body,
        method="PATCH",
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/merge-patch+json",
        },
    )
    try:
        with urllib.request.urlopen(req, context=ctx, timeout=45) as resp:
            if int(resp.status) not in (200, 201):
                raise RuntimeError(f"Kubernetes API: HTTP {resp.status}")
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", errors="replace")[:800]
        raise RuntimeError(f"Kubernetes API HTTP {e.code}: {detail}") from e


def patch_cronjob_via_kubectl(namespace: str, name: str, merge_patch: dict[str, Any]) -> None:
    if not shutil.which("kubectl"):
        raise RuntimeError("kubectl не найден в PATH")
    payload = json.dumps(merge_patch, separators=(",", ":"))
    r = subprocess.run(
        [
            "kubectl",
            "-n",
            namespace,
            "patch",
            "cronjob",
            name,
            "--type",
            "merge",
            "-p",
            payload,
        ],
        capture_output=True,
        text=True,
        timeout=120,
    )
    if r.returncode != 0:
        err = (r.stderr or r.stdout or "").strip() or f"exit {r.returncode}"
        raise RuntimeError(err)


def reconcile_egisz_monitor_sync_cronjob(
    auto_sync: Mapping[str, Any] | None,
    *,
    namespace: str | None = None,
    cronjob_name: str | None = None,
) -> tuple[bool, str]:
    """
    Применить auto_sync к CronJob в Kubernetes (suspend / schedule / timeZone).

    В поде — PATCH через API и service account; на рабочей станции — kubectl, если есть в PATH.

    Возвращает (успех, краткое сообщение для UI или лога).
    """
    ns = (namespace or os.environ.get("EGISZ_MONITOR_K8S_NAMESPACE") or "").strip() or (
        _read_in_cluster_namespace() if _in_cluster() else DEFAULT_NAMESPACE
    )
    cj = (cronjob_name or os.environ.get("EGISZ_MONITOR_SYNC_CRONJOB_NAME") or "").strip() or DEFAULT_CRONJOB_NAME
    patch = build_cronjob_merge_patch(auto_sync)
    spec = patch.get("spec") or {}
    suspend = spec.get("suspend")
    sched = spec.get("schedule")
    tz = spec.get("timeZone")
    summary = f"CronJob {cj}: suspend={suspend}, schedule={sched!r}, timeZone={tz!r}"

    try:
        if _in_cluster():
            patch_cronjob_in_cluster(ns, cj, patch)
        else:
            patch_cronjob_via_kubectl(ns, cj, patch)
    except FileNotFoundError as e:
        return False, f"CronJob не обновлён: {e}"
    except OSError as e:
        return False, f"CronJob не обновлён: {e}"
    except RuntimeError as e:
        return False, f"CronJob: {e}"
    except subprocess.TimeoutExpired:
        return False, "CronJob: kubectl patch timeout"

    return True, summary
