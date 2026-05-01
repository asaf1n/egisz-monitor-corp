"""Default Firebird extraction SQL.

Schema (PROXY_EGISZ): EXCHANGELOG (LOGTEXT = URL/хост клиники, MSGTEXT = SOAP/XML),
EGISZ_MESSAGES (DOCUMENTID, REPLYTO, MSGID), EGISZ_LICENSES (MO_UID, MO_DOMEN, JID, KIND),
JPERSONS (JNAME, JINN VARCHAR(12), FIR_OID VARCHAR(255) — как MO UID для <organization>).
KIND exists only in EGISZ_LICENSES — not on EGISZ_MESSAGES. Строка EGISZ_LICENSES: REPLYTO matches MO_DOMEN.
localUid в SOAP ↔ DOCUMENTID; клиника: gost- сначала в MSGTEXT (разбор текста сообщения), затем LOGTEXT, иначе REPLYTO → MO_DOMEN → JID → JPERSONS.

Инкремент журнала по EXCHANGELOG.LOGID; сообщения EGISZ_MESSAGES — постранично по EGMID выше курсора; лицензии — полная выгрузка EGISZ_LICENSES с LEFT JOIN JPERSONS без отбора на стороне Firebird, очистка и merge в dim_clinics в PostgreSQL; сопоставление с журналом по MSGID и REPLYTO→лицензия после выгрузки.
"""

from __future__ import annotations


def default_exchangelog_select() -> str:
    """Журнал без связи с EGISZ_MESSAGES и без фильтра по дате на источнике; ограничение по LOGID задаёт пагинация."""
    return """
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
    e.MSGID AS MSGID
FROM EXCHANGELOG e
WHERE 1=1
""".strip()


def enrichment_egisz_licenses_sql() -> str:
    """Полная выгрузка EGISZ_LICENSES с LEFT JOIN JPERSONS; фильтрация пустых строк — в PostgreSQL после staging."""
    return """
SELECT
    l.ID AS ID,
    l.JID AS JID,
    l.MO_UID AS MO_UID,
    l.MO_DOMEN AS MO_DOMEN,
    l.MODIFYDATE AS MODIFYDATE,
    l.KIND AS EGISZ_LICENSES_KIND,
    jp.JNAME AS JNAME,
    jp.JINN AS JINN,
    jp.FIR_OID AS FIR_OID
FROM EGISZ_LICENSES l
LEFT JOIN JPERSONS jp ON jp.JID = l.JID
""".strip()


def outbound_documents_staging_select(*, min_egmid: int) -> str:
    """Исходящие с DOCUMENTID; только строки с EGMID выше курсора на начало прогона (как инкрементальная выгрузка сообщений)."""
    floor = int(min_egmid)
    return f"""
SELECT
    TRIM(m.DOCUMENTID) AS DOCUMENTID,
    m.EGMID AS EGMID,
    m.CREATEDATE AS MSG_SENT_AT,
    m.REPLYTO AS REPLYTO
FROM EGISZ_MESSAGES m
WHERE m.DOCUMENTID IS NOT NULL
  AND TRIM(m.DOCUMENTID) <> ''
  AND m.EGMID > {floor}
ORDER BY m.EGMID DESC
""".strip()


def egisz_messages_incremental_sql(*, last_egmid: int, limit: int) -> str:
    """Страница EGISZ_MESSAGES: только EGMID выше курсора (инкремент без окна по дате)."""
    last = int(last_egmid)
    lim = max(1, min(int(limit), 50_000))
    return f"""
SELECT FIRST {lim}
    m.EGMID AS EGMID,
    m.MSGID AS MSGID,
    m.REPLYTO AS REPLYTO,
    TRIM(m.DOCUMENTID) AS DOCUMENTID,
    m.CREATEDATE AS MSG_CREATED_AT
FROM EGISZ_MESSAGES m
WHERE m.EGMID > {last}
ORDER BY m.EGMID
""".strip()


def exchangelog_count_logid_after_cursor(*, last_log_id: int) -> str:
    """COUNT строк EXCHANGELOG с LOGID выше курсора."""
    lid = int(last_log_id)
    return f"""
SELECT COUNT(*) AS cnt
FROM EXCHANGELOG e
WHERE e.LOGID > {lid}
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
    """Сколько строк EXCHANGELOG попадает в выборку при текущем курсоре (для прогресса ETL и кастомного source_query)."""
    lid = int(last_log_id)
    base = inner_select.strip().rstrip(";")
    return f"""
SELECT COUNT(*) AS cnt
FROM (
{base}
  AND e.LOGID > {lid}
) cnt_inner
""".strip()
