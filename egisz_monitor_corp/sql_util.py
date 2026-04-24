"""Default Firebird extraction SQL.

Schema (PROXY_EGISZ): EXCHANGELOG, EGISZ_MESSAGES (DOCUMENTID, REPLYTO), EGISZ_LICENSES
(MO_UID, MO_DOMEN, JID, KIND), JPERSONS (JNAME). KIND exists only in EGISZ_LICENSES —
not on EGISZ_MESSAGES. License row is resolved by matching REPLYTO substring to MO_DOMEN.
"""

from __future__ import annotations


def default_exchangelog_select(sync_window_days: int) -> str:
    # Scalar subqueries avoid row multiplication when several licenses match one REPLYTO.
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
    ) AS LICENSE_KIND,
    (
        SELECT FIRST 1 l2.JID
        FROM EGISZ_LICENSES l2
        WHERE m.REPLYTO IS NOT NULL
          AND l2.MO_DOMEN IS NOT NULL
          AND TRIM(l2.MO_DOMEN) <> ''
          AND POSITION(TRIM(l2.MO_DOMEN) IN TRIM(m.REPLYTO)) > 0
        ORDER BY l2.MODIFYDATE DESC, l2.ID DESC
    ) AS LICENSE_JID
FROM EXCHANGELOG e
LEFT JOIN EGISZ_MESSAGES m
    ON m.MSGID = e.MSGID
WHERE e.LOGDATE >= DATEADD(-{sync_window_days} DAY TO CURRENT_TIMESTAMP)
""".strip()


def enrichment_licenses_sql() -> str:
    return """
SELECT
    l.JID AS JID,
    l.MO_UID AS MO_UID,
    l.KIND AS LICENSE_KIND,
    jp.JNAME AS JNAME
FROM EGISZ_LICENSES l
LEFT JOIN JPERSONS jp ON jp.JID = l.JID
WHERE l.JID IS NOT NULL
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
