"""Манифест sql/schema_apply_order.txt согласован с pg_warehouse.apply_reports_schema."""

from egisz_monitor_corp.pg_warehouse import reports_schema_sql_filenames


def test_reports_schema_sql_filenames_default_bundle() -> None:
    names = reports_schema_sql_filenames()
    assert names[0] == "001_schema.sql"
    assert names[1] == "002_etl_state.sql"
    assert names[2] == "005_healthcheck.sql"
    assert len(names) == 3
