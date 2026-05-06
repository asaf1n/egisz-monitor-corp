"""Regression: field_filter_defaults.yaml loads and has expected top-level keys."""

from pathlib import Path

import yaml


def test_field_filter_defaults_yaml_exists_and_structure():
    root = Path(__file__).resolve().parents[1]
    path = root / "metabase_dashboards" / "field_filter_defaults.yaml"
    assert path.is_file(), f"missing {path}"
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert isinstance(data, dict)
    assert data.get("version") == 1
    raw = path.read_text(encoding="utf-8")
    assert "metabase-field-filters" in raw
    assert "setup-dashboards.sh" in raw
