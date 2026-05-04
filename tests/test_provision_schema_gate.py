"""Regression: dashboard provisioning must wait for v_rpt_semd_archive_ui (архив на дашборде «06»)."""

from pathlib import Path


def test_provision_sh_requires_full_schema_before_setup_dashboards() -> None:
    root = Path(__file__).resolve().parents[1]
    prov = (root / "metabase" / "provision.sh").read_text(encoding="utf-8")
    assert "v_rpt_semd_archive_ui" in prov
    assert 'if [ "${SCHEMA_CHECK}" -ge 5 ]; then' in prov


def test_provision_sh_does_not_invoke_verify_corp_stack() -> None:
    root = Path(__file__).resolve().parents[1]
    prov = (root / "metabase" / "provision.sh").read_text(encoding="utf-8")
    assert "verify-corp-stack.sh" not in prov
