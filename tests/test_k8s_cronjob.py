from __future__ import annotations

from unittest.mock import MagicMock, patch

from egisz_monitor_corp.k8s_cronjob import (
    build_cronjob_merge_patch,
    reconcile_egisz_monitor_sync_cronjob,
)


def test_build_merge_suspend_follows_enabled() -> None:
    p = build_cronjob_merge_patch({"enabled": True, "schedule_cron": "0 * * * *", "timezone": "Europe/Moscow"})
    assert p["spec"]["suspend"] is False
    assert p["spec"]["schedule"] == "0 * * * *"
    assert p["spec"]["timeZone"] == "Europe/Moscow"

    p2 = build_cronjob_merge_patch({"enabled": False})
    assert p2["spec"]["suspend"] is True


def test_reconcile_off_cluster_no_kubectl_returns_false() -> None:
    with patch("egisz_monitor_corp.k8s_cronjob._in_cluster", return_value=False), patch(
        "egisz_monitor_corp.k8s_cronjob.shutil.which", return_value=None
    ):
        ok, msg = reconcile_egisz_monitor_sync_cronjob({"enabled": True})
    assert ok is False
    assert "kubectl" in msg.lower()


def test_reconcile_off_cluster_kubectl_ok() -> None:
    with patch("egisz_monitor_corp.k8s_cronjob._in_cluster", return_value=False), patch(
        "egisz_monitor_corp.k8s_cronjob.shutil.which", return_value="/bin/kubectl"
    ), patch("egisz_monitor_corp.k8s_cronjob.subprocess.run") as run_mock:
        run_mock.return_value = MagicMock(returncode=0)
        ok, msg = reconcile_egisz_monitor_sync_cronjob({"enabled": False})
    assert ok is True
    assert "egisz-monitor-sync" in msg
    assert run_mock.call_count == 1
    args = run_mock.call_args[0][0]
    assert args[0] == "kubectl"
    assert "-n" in args
