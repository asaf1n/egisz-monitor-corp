"""Default Firebird extraction SQL.

Смысл полей в EGISZ_MESSAGES:
  • **MSGID** — идентификатор сообщения в контуре обмена/интеграций; в EXCHANGELOG ссылка на него в колонке MSGID;
    тело SOAP-колбэка при наличии — в EXCHANGELOG.MSGTEXT.
  • **EGMID** — суррогатный ключ строки в таблице EGISZ_MESSAGES (фиксация исходящего при отправке в РЭМД ЕГИСЗ).

Schema (PROXY_EGISZ): EXCHANGELOG (LOGTEXT = URL/хост клиники, MSGTEXT = SOAP/XML),
EGISZ_MESSAGES (DOCUMENTID, REPLYTO, MSGID, EGMID).

Инкремент журнала по **EXCHANGELOG.LOGID**; выборка журнала **без JOIN** к EGISZ_MESSAGES. При
`sync_window_days` > 0 к журналу добавляется окно по **LOGDATE**; при **0** или меньше — **без** предиката по
дате (синхронизация по всем записям за курсором **LOGID**).

Снимок **EGISZ_MESSAGES** (непустой **DOCUMENTID**) в **PostgreSQL** `stg_egisz_messages_journal`: при
`sync_window_days` > 0 — окно по **CREATEDATE**; при **0** или меньше — без фильтра по дате. Сопоставление с
журналом по **MSGID** — в PG (см. `etl`).
"""

from __future__ import annotations


def default_exchangelog_select() -> str:
    """Журнал EXCHANGELOG только из e (без JOIN к EGISZ_MESSAGES)."""
    return """
SELECT
    e.LOGID AS logid,
    e.LOGDATE AS logdate,
    e.LOGSTATE AS logstate,
    e.LOGTEXT AS logtext,
    e.MSGTEXT AS msgtext,
    e.METHOD AS method,
    e.URI AS uri,
    e."ACTION" AS action,
    e.PARENTLOGID AS parentlogid,
    e.GRPID AS grpid,
    e.MODIFYDATE AS modifydate,
    e.CREATEDATE AS log_created_at,
    e.MSGID AS msgid
FROM EXCHANGELOG e
WHERE 1=1
""".strip()


def exchangelog_inner_sql_for_etl(*, sync_window_days: int | None) -> str:
    """Дефолтный SELECT журнала + опционально окно по LOGDATE (как в прод-ETL `exchangelog_inner_sql_for_etl`).

    При ``sync_window_days`` ≤ 0 или ``None`` предикат по LOGDATE не добавляется (полная выборка по курсору LOGID).
    """
    base = default_exchangelog_select()
    d = int(sync_window_days) if sync_window_days is not None else 0
    if d <= 0:
        return base
    return (
        base
        + f"\n  AND e.LOGDATE >= DATEADD(-{d} DAY TO CURRENT_TIMESTAMP)"
    )


# Верхняя граница FIRST n в Firebird для страниц журнала и снимка EGISZ_MESSAGES (см. etl.batch_size).
_FB_FIRST_ROWS_HARD_CAP = 65_000


def _clamp_fb_first_limit(limit: int) -> int:
    return max(1, min(int(limit), _FB_FIRST_ROWS_HARD_CAP))


def egisz_messages_documentid_filled_predicate(*, table_alias: str = "m") -> str:
    """Строки с привязкой к документу СЭМД; без этого — сервисные/служебные сообщения в контуре."""
    a = table_alias.strip() or "m"
    # Без TRIM(...) в предикате: на больших таблицах это часто мешает использованию индекса по DOCUMENTID.
    return f"{a}.DOCUMENTID IS NOT NULL AND {a}.DOCUMENTID <> ''"


def egisz_messages_createdate_window_sql(*, sync_window_days: int | None, table_alias: str = "m") -> str:
    """Доп. AND к запросу EGISZ_MESSAGES: окно по CREATEDATE (как у журнала EXCHANGELOG по LOGDATE).

    При ``sync_window_days`` ≤ 0 или ``None`` строка не добавляется (без фильтра по дате).
    """
    d = int(sync_window_days) if sync_window_days is not None else 0
    if d <= 0:
        return ""
    a = table_alias.strip() or "m"
    return f"\n  AND {a}.CREATEDATE >= DATEADD(-{d} DAY TO CURRENT_TIMESTAMP)"


def journal_messages_staging_base_sql(*, sync_window_days: int | None) -> str:
    """Базовый SELECT EGISZ_MESSAGES для снимка в PostgreSQL (тот же предикат, что и у `journal_messages_keyset_page_sql`).

    Те же отборы, что и для исходящих (`outbound_documents_staging_select`): непустой DOCUMENTID и то же окно CREATEDATE.
    """
    doc = egisz_messages_documentid_filled_predicate()
    date_clause = egisz_messages_createdate_window_sql(sync_window_days=sync_window_days)
    return f"""
SELECT
    m.MSGID AS msgid,
    m.EGMID AS egmid,
    m.REPLYTO AS replyto,
    TRIM(m.DOCUMENTID) AS documentid,
    m.CREATEDATE AS msg_created_at
FROM EGISZ_MESSAGES m
WHERE {doc}
{date_clause}
""".strip()


def journal_messages_keyset_page_sql(
    *,
    sync_window_days: int | None,
    after_egmid: int,
    limit: int,
) -> str:
    """Firebird: страница снимка EGISZ_MESSAGES с EGMID строго больше after_egmid (ключевая пагинация).

    Запрос **одноуровневый** (без обёртки ``SELECT FIRST … FROM (SELECT … ORDER BY)``): на больших
    ``EGISZ_MESSAGES`` вложенный вариант часто приводит к полной сортировке/материализации подзапроса
    до применения ``FIRST``, тогда как ``SELECT FIRST n … ORDER BY m.EGMID`` позволяет оптимизатору
    остановиться после *n* подходящих строк при наличии индекса по **EGMID**.
    """
    low = max(0, int(after_egmid))
    lim = _clamp_fb_first_limit(limit)
    doc = egisz_messages_documentid_filled_predicate()
    date_clause = egisz_messages_createdate_window_sql(sync_window_days=sync_window_days)
    return f"""
SELECT FIRST {lim}
    m.MSGID AS msgid,
    m.EGMID AS egmid,
    m.REPLYTO AS replyto,
    TRIM(m.DOCUMENTID) AS documentid,
    m.CREATEDATE AS msg_created_at
FROM EGISZ_MESSAGES m
WHERE {doc}
{date_clause}
  AND m.EGMID > {low}
ORDER BY m.EGMID
""".strip()


def outbound_documents_staging_select(*, sync_window_days: int | None) -> str:
    """Исходящие с DOCUMENTID: полное окно по CREATEDATE (как sync_window_days у журнала); при 0/null — без даты.

    После каждого успешного sync staging перезаписывается целиком (DELETE + INSERT). Предикат по дате,
    а не по last_egmid (курсор keyset снимка в etl_state), сохраняет корректный
    снимок при удалении строк в Firebird.
    """
    doc = egisz_messages_documentid_filled_predicate()
    date_clause = egisz_messages_createdate_window_sql(sync_window_days=sync_window_days)
    return f"""
SELECT
    TRIM(m.DOCUMENTID) AS DOCUMENTID,
    m.EGMID AS EGMID,
    m.CREATEDATE AS MSG_SENT_AT,
    m.REPLYTO AS REPLYTO
FROM EGISZ_MESSAGES m
WHERE {doc}
{date_clause}
ORDER BY m.EGMID DESC
""".strip()


def default_jpersons_select_sql() -> str:
    """Полная выборка JPERSONS для staging (без JOIN в Firebird)."""
    return """
SELECT
    j.JID AS jid,
    j.JNAME AS jname,
    j.JINN AS jinn,
    j.FIR_OID AS fir_oid
FROM JPERSONS j
WHERE j.JID IS NOT NULL AND j.JID > 0
""".strip()


def default_egisz_licenses_select_sql() -> str:
    """Полная выборка EGISZ_LICENSES для staging (без JOIN в Firebird)."""
    return """
SELECT
    l.ID AS id,
    l.JID AS jid,
    l.MO_UID AS mo_uid,
    l.MO_DOMEN AS mo_domen,
    l.MODIFYDATE AS modifydate,
    l.KIND AS egisz_licenses_kind
FROM EGISZ_LICENSES l
WHERE l.JID IS NOT NULL AND l.JID > 0
""".strip()


def egisz_messages_by_msgids_sql(placeholders: str) -> str:
    """SELECT по списку MSGID; placeholders — строка вида '?,?,?' (длина = число MSGID). Диагностика / ручные запросы."""
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
