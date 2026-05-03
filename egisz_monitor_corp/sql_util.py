"""Default Firebird extraction SQL.

Schema (PROXY_EGISZ): EXCHANGELOG (LOGTEXT = URL/хост клиники, MSGTEXT = SOAP/XML),
EGISZ_MESSAGES (DOCUMENTID, REPLYTO, MSGID, EGMID), EGISZ_LICENSES (MO_UID, MO_DOMEN, JID, KIND),
JPERSONS (JNAME, JINN VARCHAR(12), FIR_OID VARCHAR(255) — как MO UID для <organization>).
KIND exists only in EGISZ_LICENSES — not on EGISZ_MESSAGES. Строка EGISZ_LICENSES: REPLYTO matches MO_DOMEN.
localUid в SOAP ↔ DOCUMENTID; **JID клиники:** gost- только в **EXCHANGELOG.LOGTEXT** и **EGISZ_MESSAGES.REPLYTO** (не в MSGTEXT); затем **EGISZ_LICENSES** по REPLYTO → **JID**; **JPERSONS** по JID для наименования.

Инкремент журнала по EXCHANGELOG.LOGID; в выборке журнала — LEFT JOIN EGISZ_MESSAGES по MSGID (поле MESSAGE_EGMID).
Сообщения дополнительно кэшируются постранично по EGMID выше курсора; лицензии — полная выгрузка EGISZ_LICENSES без JOIN в Firebird; JPERSONS отдельно; сшивка JNAME/JINN/FIR_OID в PostgreSQL (`stg_jpersons_import` + `UPDATE … FROM`); merge в dim_clinics в PostgreSQL; сопоставление с журналом по MSGID и REPLYTO→лицензия после выгрузки.
"""


from __future__ import annotations


def default_exchangelog_select() -> str:
    """Журнал EXCHANGELOG; EGMID строки EGISZ_MESSAGES — через LEFT JOIN по MSGID (как LOGID в той же выборке)."""
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
    e.MSGID AS MSGID,
    m.EGMID AS MESSAGE_EGMID
FROM EXCHANGELOG e
LEFT JOIN EGISZ_MESSAGES m ON m.MSGID = e.MSGID
WHERE 1=1
""".strip()


def exchangelog_inner_sql_for_etl(*, sync_window_days: int | None) -> str:
    """Дефолтный SELECT журнала + опционально окно по LOGDATE (как в sql/003_diagnostic_counts_firebird.sql)."""
    base = default_exchangelog_select()
    d = int(sync_window_days) if sync_window_days is not None else 0
    if d <= 0:
        return base
    return (
        base
        + f"\n  AND e.LOGDATE >= DATEADD(-{d} DAY TO CURRENT_TIMESTAMP)"
    )


def jpersons_all_sql() -> str:
    """Все юрлица для сшивки с лицензиями в Python (до выгрузки EGISZ_LICENSES)."""
    return """
SELECT
    jp.JID AS JID,
    jp.JNAME AS JNAME,
    jp.JINN AS JINN,
    jp.FIR_OID AS FIR_OID
FROM JPERSONS jp
WHERE jp.JID IS NOT NULL
""".strip()


def enrichment_egisz_licenses_only_sql() -> str:
    """Полная выгрузка EGISZ_LICENSES без JOIN; JNAME/JINN/FIR_OID подставляются из кэша JPERSONS в ETL."""
    return """
SELECT
    l.ID AS ID,
    l.JID AS JID,
    l.MO_UID AS MO_UID,
    l.MO_DOMEN AS MO_DOMEN,
    l.MODIFYDATE AS MODIFYDATE,
    l.KIND AS EGISZ_LICENSES_KIND
FROM EGISZ_LICENSES l
""".strip()


def enrichment_egisz_licenses_sql() -> str:
    """Обратная совместимость: один SQL с JOIN (устарело — ETL использует JPERSONS + EGISZ_LICENSES по отдельности)."""
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


# Верхняя граница FIRST n в Firebird для страниц журнала/сообщений (см. etl.interleave_page_rows).
_FB_FIRST_ROWS_HARD_CAP = 65_000


def _clamp_fb_first_limit(limit: int) -> int:
    return max(1, min(int(limit), _FB_FIRST_ROWS_HARD_CAP))


def _sync_window_sql_fragment(sync_window_days: int | None) -> str:
    d = int(sync_window_days) if sync_window_days is not None else 0
    if d <= 0:
        return ""
    return f"\n  AND m.CREATEDATE >= DATEADD(-{d} DAY TO CURRENT_TIMESTAMP)"


def egisz_messages_incremental_sql(
    *, last_egmid: int, limit: int, sync_window_days: int | None = None
) -> str:
    """Страница EGISZ_MESSAGES: EGMID выше курсора; опционально CREATEDATE в пределах sync_window_days."""
    last = int(last_egmid)
    lim = _clamp_fb_first_limit(limit)
    win = _sync_window_sql_fragment(sync_window_days)
    return f"""
SELECT FIRST {lim}
    m.EGMID AS EGMID,
    m.MSGID AS MSGID,
    m.REPLYTO AS REPLYTO,
    TRIM(m.DOCUMENTID) AS DOCUMENTID,
    m.CREATEDATE AS MSG_CREATED_AT
FROM EGISZ_MESSAGES m
WHERE m.EGMID > {last}{win}
ORDER BY m.EGMID
""".strip()


def egisz_messages_incremental_page_max_egmid_sql(
    *, last_egmid: int, limit: int, sync_window_days: int | None = None
) -> str:
    """MAX(EGMID) по той же странице FIRST n, что и egisz_messages_incremental_sql (обход «залипания» курсора в драйвере)."""
    last = int(last_egmid)
    lim = _clamp_fb_first_limit(limit)
    win = _sync_window_sql_fragment(sync_window_days)
    return f"""
SELECT MAX(p.EGMID) AS max_egmid
FROM (
    SELECT FIRST {lim} m.EGMID AS EGMID
    FROM EGISZ_MESSAGES m
    WHERE m.EGMID > {last}{win}
    ORDER BY m.EGMID
) p
""".strip()


def exchangelog_count_logid_after_cursor(*, last_log_id: int) -> str:
    """COUNT строк EXCHANGELOG с LOGID выше курсора."""
    lid = int(last_log_id)
    return f"""
SELECT COUNT(*) AS cnt
FROM EXCHANGELOG e
WHERE e.LOGID > {lid}
""".strip()


def egisz_messages_by_msgids_sql(placeholders: str) -> str:
    """SELECT по списку MSGID; placeholders — строка вида '?,?,?' (длина = число MSGID)."""
    return f"""
SELECT
    m.EGMID AS EGMID,
    m.MSGID AS MSGID,
    m.REPLYTO AS REPLYTO,
    TRIM(m.DOCUMENTID) AS DOCUMENTID,
    m.CREATEDATE AS MSG_CREATED_AT
FROM EGISZ_MESSAGES m
WHERE m.MSGID IN ({placeholders})
""".strip()


def paginated_exchangelog_sql(inner_select: str, *, last_log_id: int, limit: int) -> str:
    """Firebird: FIRST n rows with LOGID > cursor, ordered by LOGID (incremental, not MODIFYDATE)."""
    lid = int(last_log_id)
    lim = _clamp_fb_first_limit(limit)
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
