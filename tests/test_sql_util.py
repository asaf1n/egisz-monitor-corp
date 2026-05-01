from egisz_monitor_corp.sql_util import (
    default_exchangelog_select,
    egisz_messages_by_msgids_sql,
    egisz_messages_incremental_sql,
    enrichment_egisz_licenses_only_sql,
    enrichment_egisz_licenses_sql,
    exchangelog_count_after_cursor,
    exchangelog_count_logid_after_cursor,
    jpersons_all_sql,
    outbound_documents_staging_select,
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


def test_enrichment_licenses_only_sql_no_join() -> None:
    s = enrichment_egisz_licenses_only_sql()
    assert "FROM EGISZ_LICENSES" in s
    assert "JOIN" not in s.upper()
    assert "DATEADD" not in s


def test_jpersons_all_sql_selects_jid() -> None:
    s = jpersons_all_sql()
    assert "FROM JPERSONS" in s.upper()
    assert "JID IS NOT NULL" in s


def test_egisz_messages_by_msgids_uses_placeholders() -> None:
    s = egisz_messages_by_msgids_sql("?,?")
    assert "IN (?,?)" in s.replace("\n", " ")


def test_enrichment_licenses_sql_join_still_available() -> None:
    s = enrichment_egisz_licenses_sql()
    assert "FROM EGISZ_LICENSES" in s
    assert "JOIN JPERSONS" in s.upper()
    assert "DATEADD" not in s
    assert "WHERE l.JID" not in s


def test_default_count_logid_only_no_join() -> None:
    sql = exchangelog_count_logid_after_cursor(last_log_id=0)
    assert "COUNT(*)" in sql
    assert "EXCHANGELOG" in sql
    assert "LOGID > 0" in sql
    assert "EGISZ_MESSAGES" not in sql
    assert "LOGDATE" not in sql


def test_default_select_is_exchangelog_only_with_msgid() -> None:
    s = default_exchangelog_select()
    assert "FROM EXCHANGELOG e" in s
    assert "e.MSGID AS MSGID" in s
    assert "EGISZ_MESSAGES" not in s
    assert "DATEADD" not in s


def test_outbound_staging_select_orders_by_egmid_desc_uses_egmid_floor() -> None:
    s = outbound_documents_staging_select(min_egmid=14)
    assert "ORDER BY m.EGMID DESC" in s
    assert "EGMID > 14" in s
    assert "EGISZ_LICENSES" not in s
    assert "DATEADD" not in s


def test_egisz_messages_incremental_orders_by_egmid() -> None:
    s = egisz_messages_incremental_sql(last_egmid=99, limit=100)
    assert "FIRST 100" in s
    assert "EGMID > 99" in s
    assert "ORDER BY m.EGMID" in s
    assert "DATEADD" not in s
