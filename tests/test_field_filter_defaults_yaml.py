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
    assert "dwh_date_bindings" in data
    assert "text_parameter_bindings" in data
    assert isinstance(data.get("dashboards"), list)
    assert len(data["dashboards"]) >= 6
