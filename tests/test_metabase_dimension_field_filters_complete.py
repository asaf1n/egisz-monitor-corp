"""Регрессия: у каждого native template-tag type=dimension должен быть metabase-field-filters."""

import json
from pathlib import Path


def test_all_native_dimension_tags_have_metabase_field_filters() -> None:
    root = Path(__file__).resolve().parents[1] / "metabase_dashboards"
    for path in sorted(root.glob("*.json")):
        data = json.loads(path.read_text(encoding="utf-8"))
        for card in data.get("cards", []):
            tags = ((card.get("dataset_query") or {}).get("native") or {}).get("template-tags") or {}
            ff = card.get("metabase-field-filters") or {}
            for name, spec in tags.items():
                if (spec or {}).get("type") != "dimension":
                    continue
                assert name in ff, (
                    f"{path.name} card {card.get('name')!r}: "
                    f"dimension tag {name!r} missing metabase-field-filters"
                )
                entry = ff[name]
                assert "table_ref" in entry and "field_name" in entry, (
                    f"{path.name} card {card.get('name')!r}: incomplete ff for {name!r}"
                )
