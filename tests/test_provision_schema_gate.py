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


def test_provision_sh_auto_mode_uses_dashboard_bundle_sha_stamp() -> None:
    """METABASE_FORCE_PROVISION=auto skips import only when stamp matches SHA of all metabase_dashboards/*.json."""
    root = Path(__file__).resolve().parents[1]
    prov = (root / "metabase" / "provision.sh").read_text(encoding="utf-8")
    assert "corp_mb_compute_dashboards_bundle_sha" in prov
    assert "MANIFEST_STAMP_FILE" in prov
    assert "corp-metabase-dashboards-manifest.sha256" in prov


def test_default_metabase_dashboard_json_file_count_is_six() -> None:
    """Проект по умолчанию: шесть дашбордов Metabase (01–06) — по одному JSON на дашборд."""
    root = Path(__file__).resolve().parents[1]
    dash_dir = root / "metabase_dashboards"
    names = sorted(p.name for p in dash_dir.glob("*.json"))
    assert len(names) == 6, names
