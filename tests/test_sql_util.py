from egisz_monitor_corp.sql_util import default_exchangelog_select, paginated_exchangelog_sql


def test_paginated_wraps_inner_and_filters_logid() -> None:
    inner = "SELECT 1 AS x FROM T t WHERE 1=1"
    sql = paginated_exchangelog_sql(inner, last_log_id=100, limit=50)
    assert "FIRST 50" in sql
    assert "LOGID > 100" in sql
    assert "ORDER BY e.LOGID" in sql


def test_default_select_contains_licenses_only_kind() -> None:
    s = default_exchangelog_select(7)
    assert "LICENSE_KIND" in s
    assert "EGISZ_LICENSES" in s
    assert "DATEADD(-7 DAY" in s
