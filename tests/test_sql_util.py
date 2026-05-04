from egisz_monitor_corp.sql_util import (
    default_exchangelog_select,
    egisz_messages_by_msgids_sql,
    egisz_messages_documentid_filled_predicate,
    exchangelog_count_after_cursor,
    exchangelog_count_logid_after_cursor,
    exchangelog_inner_sql_for_etl,
    journal_messages_keyset_page_sql,
    journal_messages_staging_base_sql,
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


def test_egisz_messages_by_msgids_uses_placeholders() -> None:
    s = egisz_messages_by_msgids_sql("?,?")
    assert "IN (?,?)" in s.replace("\n", " ")


def test_default_count_logid_only_no_join() -> None:
    sql = exchangelog_count_logid_after_cursor(last_log_id=0)
    assert "COUNT(*)" in sql
    assert "EXCHANGELOG" in sql
    assert "LOGID > 0" in sql
    assert "EGISZ_MESSAGES" not in sql
    assert "LOGDATE" not in sql


def test_default_select_exchangelog_no_join_messages_in_pg() -> None:
    s = default_exchangelog_select()
    assert "FROM EXCHANGELOG e" in s
    assert "e.MSGID AS msgid" in s.replace("\n", " ")
    assert "LEFT JOIN" not in s
    assert "EGISZ_MESSAGES" not in s


def test_journal_messages_keyset_page_sql_filters_after_egmid() -> None:
    sql = journal_messages_keyset_page_sql(sync_window_days=None, after_egmid=42_000, limit=500)
    assert "AND m.EGMID > 42000" in sql.replace("\n", " ")
    assert "FIRST 500" in sql.replace("\n", " ")
    assert "ORDER BY m.EGMID" in sql.replace("\n", " ")
    assert "TRIM(m.DOCUMENTID)" in sql
    assert "FROM EGISZ_MESSAGES m" in sql
    assert "FROM (" not in sql


def test_journal_messages_base_matches_outbound_predicates() -> None:
    j = journal_messages_staging_base_sql(sync_window_days=7)
    o = outbound_documents_staging_select(sync_window_days=7)
    pred = egisz_messages_documentid_filled_predicate()
    assert pred in j.replace("\n", " ")
    assert pred in o.replace("\n", " ")
    assert "DATEADD(-7 DAY TO CURRENT_TIMESTAMP)" in j
    assert "DATEADD(-7 DAY TO CURRENT_TIMESTAMP)" in o


def test_exchangelog_inner_for_etl_adds_logdate_window() -> None:
    s = exchangelog_inner_sql_for_etl(sync_window_days=14)
    assert "LOGDATE >= DATEADD(-14 DAY TO CURRENT_TIMESTAMP)" in s.replace("\n", " ")


def test_exchangelog_inner_for_etl_zero_means_no_extra_predicate() -> None:
    s = exchangelog_inner_sql_for_etl(sync_window_days=0)
    assert "DATEADD" not in s


def test_outbound_staging_select_orders_by_egmid_desc_uses_createdate_window() -> None:
    s = outbound_documents_staging_select(sync_window_days=14)
    assert "ORDER BY m.EGMID DESC" in s
    assert "CREATEDATE >= DATEADD(-14 DAY TO CURRENT_TIMESTAMP)" in s.replace("\n", " ")
    assert "EGISZ_LICENSES" not in s


def test_outbound_staging_select_zero_days_no_date_predicate() -> None:
    s = outbound_documents_staging_select(sync_window_days=0)
    assert "ORDER BY m.EGMID DESC" in s
    assert "DATEADD" not in s


def test_outbound_staging_select_none_sync_days_no_date_predicate() -> None:
    s = outbound_documents_staging_select(sync_window_days=None)
    assert "DATEADD" not in s
