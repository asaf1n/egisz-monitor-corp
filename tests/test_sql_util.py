from egisz_monitor_corp.sql_util import (
    default_exchangelog_select,
    exchangelog_count_after_cursor,
    paginated_exchangelog_sql,
)


def test_paginated_wraps_inner_and_filters_logid() -> None:
    inner = "SELECT 1 AS x FROM T t WHERE 1=1"
    sql = paginated_exchangelog_sql(inner, last_log_id=100, limit=50)
    assert "FIRST 50" in sql
    assert "LOGID > 100" in sql
    assert "ORDER BY e.LOGID" in sql


def test_count_wraps_inner_and_filters_logid() -> None:
    inner = "SELECT 1 AS x FROM T t WHERE 1=1"
    sql = exchangelog_count_after_cursor(inner, last_log_id=42)
    assert "COUNT(*)" in sql
    assert "cnt_inner" in sql
    assert "LOGID > 42" in sql


def test_default_select_contains_egisz_licenses_columns() -> None:
    s = default_exchangelog_select(7)
    assert "EGISZ_LICENSES_KIND" in s
    assert "EGISZ_LICENSES_JID" in s
    assert "EGISZ_LICENSES" in s
    assert "DATEADD(-7 DAY" in s
