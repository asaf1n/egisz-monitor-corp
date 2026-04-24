"""Default Firebird extraction SQL.

Schema (PROXY_EGISZ): EXCHANGELOG (LOGTEXT = URL/хост клиники, MSGTEXT = SOAP/XML),
EGISZ_MESSAGES (DOCUMENTID, REPLYTO), EGISZ_LICENSES (MO_UID, MO_DOMEN, JID, KIND),
JPERSONS (JNAME, JINN VARCHAR(12), FIR_OID VARCHAR(255) — как MO UID для <organization>).
KIND exists only in EGISZ_LICENSES — not on EGISZ_MESSAGES. Строка EGISZ_LICENSES: REPLYTO matches MO_DOMEN.
localUid в SOAP ↔ DOCUMENTID; клиника: gost- в LOGTEXT, иначе REPLYTO → MO_DOMEN → JID → JPERSONS.
"""

from __future__ import annotations


def default_exchangelog_select(sync_window_days: int) -> str:
    # Scalar subqueries: несколько строк EGISZ_LICENSES на один REPLYTO — берём FIRST 1 по дате/ID.
    return f"""
SELECT
    e.LOGID AS LOGID,
    e.LOGDATE AS LOGDATE,
    e.LOGSTATE AS LOGSTATE,
    e.LOGTEXT AS LOGTEXT,
    e.MSGTEXT AS MSGTEXT,
    e.METHOD AS METHOD,
    e.URI AS URI,
    e."ACTION" AS ACTION,
    e.PARENTLOGID AS PARENTLOGID,
    e.GRPID AS GRPID,
    e.MODIFYDATE AS MODIFYDATE,
    e.CREATEDATE AS LOG_CREATED_AT,
    m.REPLYTO AS REPLYTO,
    m.DOCUMENTID AS DOCUMENTID,
    (
        SELECT FIRST 1 l2.MO_UID
        FROM EGISZ_LICENSES l2
        WHERE m.REPLYTO IS NOT NULL
          AND l2.MO_DOMEN IS NOT NULL
          AND TRIM(l2.MO_DOMEN) <> ''
          AND POSITION(TRIM(l2.MO_DOMEN) IN TRIM(m.REPLYTO)) > 0
        ORDER BY l2.MODIFYDATE DESC, l2.ID DESC
    ) AS MO_UID,
    (
        SELECT FIRST 1 l2.KIND
        FROM EGISZ_LICENSES l2
        WHERE m.REPLYTO IS NOT NULL
          AND l2.MO_DOMEN IS NOT NULL
          AND TRIM(l2.MO_DOMEN) <> ''
          AND POSITION(TRIM(l2.MO_DOMEN) IN TRIM(m.REPLYTO)) > 0
        ORDER BY l2.MODIFYDATE DESC, l2.ID DESC
    ) AS EGISZ_LICENSES_KIND,
    (
        SELECT FIRST 1 l2.JID
        FROM EGISZ_LICENSES l2
        WHERE m.REPLYTO IS NOT NULL
          AND l2.MO_DOMEN IS NOT NULL
          AND TRIM(l2.MO_DOMEN) <> ''
          AND POSITION(TRIM(l2.MO_DOMEN) IN TRIM(m.REPLYTO)) > 0
        ORDER BY l2.MODIFYDATE DESC, l2.ID DESC
    ) AS EGISZ_LICENSES_JID
FROM EXCHANGELOG e
LEFT JOIN EGISZ_MESSAGES m
    ON m.MSGID = e.MSGID
WHERE e.LOGDATE >= DATEADD(-{sync_window_days} DAY TO CURRENT_TIMESTAMP)
""".strip()


def enrichment_egisz_licenses_sql() -> str:
    """Все строки EGISZ_LICENSES с JID; KIND и join к JPERSONS по JID."""
    return """
SELECT
    l.JID AS JID,
    l.MO_UID AS MO_UID,
    l.KIND AS EGISZ_LICENSES_KIND,
    jp.JNAME AS JNAME,
    jp.JINN AS JINN,
    jp.FIR_OID AS FIR_OID
FROM EGISZ_LICENSES l
LEFT JOIN JPERSONS jp ON jp.JID = l.JID
WHERE l.JID IS NOT NULL
""".strip()


def enrichment_jpersons_sql() -> str:
    """JPERSONS: JNAME, ИНН (JINN), OID МО (FIR_OID; сопоставим с EGISZ_LICENSES.MO_UID / <organization>)."""
    return """
SELECT
    jp.JID AS JID,
    jp.JNAME AS JNAME,
    jp.JINN AS JINN,
    jp.FIR_OID AS FIR_OID
FROM JPERSONS jp
WHERE jp.JID IS NOT NULL
""".strip()


def outbound_documents_staging_select(sync_window_days: int) -> str:
    """EGISZ_MESSAGES с DOCUMENTID за окно: тип/клиника через REPLYTO→EGISZ_LICENSES как в EXCHANGELOG."""
    return f"""
SELECT
    TRIM(m.DOCUMENTID) AS DOCUMENTID,
    m.CREATEDATE AS MSG_SENT_AT,
    m.REPLYTO AS REPLYTO,
    (
        SELECT FIRST 1 l2.KIND
        FROM EGISZ_LICENSES l2
        WHERE m.REPLYTO IS NOT NULL
          AND l2.MO_DOMEN IS NOT NULL
          AND TRIM(l2.MO_DOMEN) <> ''
          AND POSITION(TRIM(l2.MO_DOMEN) IN TRIM(m.REPLYTO)) > 0
        ORDER BY l2.MODIFYDATE DESC, l2.ID DESC
    ) AS EGISZ_LICENSES_KIND,
    (
        SELECT FIRST 1 l2.JID
        FROM EGISZ_LICENSES l2
        WHERE m.REPLYTO IS NOT NULL
          AND l2.MO_DOMEN IS NOT NULL
          AND TRIM(l2.MO_DOMEN) <> ''
          AND POSITION(TRIM(l2.MO_DOMEN) IN TRIM(m.REPLYTO)) > 0
        ORDER BY l2.MODIFYDATE DESC, l2.ID DESC
    ) AS EGISZ_LICENSES_JID
FROM EGISZ_MESSAGES m
WHERE m.DOCUMENTID IS NOT NULL
  AND TRIM(m.DOCUMENTID) <> ''
  AND m.CREATEDATE >= DATEADD(-{sync_window_days} DAY TO CURRENT_TIMESTAMP)
""".strip()


def paginated_exchangelog_sql(inner_select: str, *, last_log_id: int, limit: int) -> str:
    """Firebird: FIRST n rows with LOGID > cursor, ordered by LOGID (incremental, not MODIFYDATE)."""
    lid = int(last_log_id)
    lim = max(1, min(int(limit), 50_000))
    base = inner_select.strip().rstrip(";")
    return f"""
SELECT FIRST {lim} src.*
FROM (
{base}
  AND e.LOGID > {lid}
ORDER BY e.LOGID
) src
""".strip()


def exchangelog_count_after_cursor(inner_select: str, *, last_log_id: int) -> str:
    """Сколько строк EXCHANGELOG попадает в выборку при текущем курсоре (для прогресса ETL)."""
    lid = int(last_log_id)
    base = inner_select.strip().rstrip(";")
    return f"""
SELECT COUNT(*) AS cnt
FROM (
{base}
  AND e.LOGID > {lid}
) cnt_inner
""".strip()
