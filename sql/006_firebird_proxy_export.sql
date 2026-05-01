-- Выгрузки с PROXY_EGISZ.FDB (Firebird). Подставьте N (дней), LAST_LOG_ID (из PostgreSQL etl_state), при необходимости лимит.
-- Для ручной выгрузки журнала см. sql_util.default_exchangelog_select (только EXCHANGELOG).

-- =============================================================================
-- Три таблицы: не более 65 000 строк строго ПОСЛЕ сохранённого «хвоста» синка.
-- Замените три нуля на свои курсоры (один раз скопировать из PostgreSQL / с прошлой выгрузки):
--   EXCHANGELOG      → last_log_id:  SELECT last_log_id FROM etl_state WHERE pipeline = 'firebird_exchangelog';
--   EGISZ_MESSAGES   → last_egmid:  SELECT last_egmid FROM etl_state WHERE pipeline = 'firebird_exchangelog';
--   EGISZ_LICENSES   → в etl_state курсора нет; укажите MAX(id) с прошлого файла или 0 для полной выборки с начала (осторожно: объём).
-- =============================================================================
SELECT FIRST 65000 *
FROM EXCHANGELOG
WHERE LOGID > 0
ORDER BY LOGID;

SELECT FIRST 65000 *
FROM EGISZ_MESSAGES
WHERE EGMID > 0
ORDER BY EGMID;

SELECT FIRST 65000 *
FROM EGISZ_LICENSES
WHERE MODIFYDATE > 0
ORDER BY MODIFYDATE;

-- --- «Верхушки» для сверки с UI / ETL ---
SELECT
    (SELECT MAX(e.LOGID)       FROM EXCHANGELOG e)       AS max_logid,
    (SELECT MAX(e.LOGDATE)     FROM EXCHANGELOG e)     AS max_logdate,
    (SELECT MAX(m.EGMID)      FROM EGISZ_MESSAGES m)    AS max_egmid,
    (SELECT MAX(m.CREATEDATE) FROM EGISZ_MESSAGES m)   AS max_msg_created,
    (SELECT MAX(l.ID)         FROM EGISZ_LICENSES l)   AS max_license_id,
    (SELECT MAX(l.MODIFYDATE) FROM EGISZ_LICENSES l)   AS max_license_modify
FROM RDB$DATABASE;

-- --- Минимальный EGMID среди сообщений в календарном окне (граница PK-диапазона; CREATEDATE в WHERE остаётся источником истины) ---
-- Замените 30 на N = sync_window_days.
SELECT MIN(m.EGMID) AS min_egmid_in_createdate_window
FROM EGISZ_MESSAGES m
WHERE m.CREATEDATE >= DATEADD(-30 DAY TO CURRENT_TIMESTAMP);

-- --- Исходящие с DOCUMENTID за окно (как ETL outbound; порядок EGMID DESC — дедуп по документу «новее первым» в DBeaver/скриптах) ---
-- Замените 30 на N.
SELECT
    TRIM(m.DOCUMENTID) AS DOCUMENTID,
    m.EGMID AS EGMID,
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
  AND m.CREATEDATE >= DATEADD(-30 DAY TO CURRENT_TIMESTAMP)
ORDER BY m.EGMID DESC;

-- --- Последние 65 000 строк журнала в окне по LOGDATE (новые LOGID первыми); N и лимит подставьте при необходимости ---
SELECT FIRST 65000 src.*
FROM (
    SELECT
        e.LOGID,
        e.LOGDATE,
        e.LOGSTATE,
        e.LOGTEXT,
        e.MSGTEXT,
        e.METHOD,
        e.URI,
        e."ACTION",
        e.PARENTLOGID,
        e.GRPID,
        e.MODIFYDATE,
        e.CREATEDATE AS LOG_CREATED_AT,
        m.REPLYTO,
        m.DOCUMENTID,
        m.CREATEDATE AS MSG_CREATED_AT,
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
    LEFT JOIN EGISZ_MESSAGES m ON m.MSGID = e.MSGID
    WHERE e.LOGDATE >= DATEADD(-30 DAY TO CURRENT_TIMESTAMP)
    ORDER BY e.LOGID DESC
) src;

-- --- Инкрементальный срез журнала после курсора (как ETL: LOGDATE окно + LOGID > LAST_LOG_ID), пакет 50 000 ---
-- LAST_LOG_ID: SELECT last_log_id FROM etl_state WHERE pipeline = 'firebird_exchangelog';
SELECT FIRST 50000 src.*
FROM (
    SELECT
        e.LOGID,
        e.LOGDATE,
        e.LOGSTATE,
        e.LOGTEXT,
        e.MSGTEXT,
        e.METHOD,
        e.URI,
        e."ACTION",
        e.PARENTLOGID,
        e.GRPID,
        e.MODIFYDATE,
        e.CREATEDATE AS LOG_CREATED_AT,
        m.REPLYTO,
        m.DOCUMENTID,
        m.CREATEDATE AS MSG_CREATED_AT,
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
    LEFT JOIN EGISZ_MESSAGES m ON m.MSGID = e.MSGID
    WHERE e.LOGDATE >= DATEADD(-30 DAY TO CURRENT_TIMESTAMP)
      AND e.LOGID > 0
    ORDER BY e.LOGID
) src;

-- Рекомендация по индексу на прокси (выполнять вручную на боевой БД при согласовании с админом):
-- CREATE INDEX EGISZ_MESSAGES_CREATEDATE ON EGISZ_MESSAGES (CREATEDATE);
