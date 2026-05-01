-- Диагностические COUNT для сверки с ETL (предикаты как в sql_util.py).
-- Подставьте N = etl.sync_window_days из актуального config/egisz_monitor.yaml (или ConfigMap / UI).
-- Для запросов с курсором подставьте LAST_LOG_ID из PostgreSQL: SELECT last_log_id FROM etl_state WHERE pipeline = 'firebird_exchangelog';

-- --- Журнал EXCHANGELOG: строки в окне по LOGDATE (без учёта курсора LOGID) ---
SELECT COUNT(*) AS exchangelog_rows_in_logdate_window
FROM EXCHANGELOG e
WHERE e.LOGDATE >= DATEADD(-30 DAY TO CURRENT_TIMESTAMP);
-- Замените 30 на N.

-- --- Журнал: строки в окне по LOGDATE И с LOGID строго больше курсора (как инкрементальный прогон) ---
SELECT COUNT(*) AS exchangelog_rows_after_cursor
FROM EXCHANGELOG e
LEFT JOIN EGISZ_MESSAGES m ON m.MSGID = e.MSGID
WHERE e.LOGDATE >= DATEADD(-30 DAY TO CURRENT_TIMESTAMP)
  AND e.LOGID > 0;
-- Замените 30 на N; 0 на LAST_LOG_ID из etl_state (для full_scan старт с 0).

-- Лёгкий вариант без JOIN (только EXCHANGELOG), совместим с exchangelog_count_logid_after_cursor / дефолтным ETL:
SELECT COUNT(*) AS exchangelog_rows_after_cursor_simple
FROM EXCHANGELOG e
WHERE e.LOGDATE >= DATEADD(-30 DAY TO CURRENT_TIMESTAMP)
  AND e.LOGID > 0;
-- Замените 30 на N и 0 на LAST_LOG_ID.

-- --- Исходящие сообщения: то же окно CREATEDATE, что outbound_documents_staging_select (сортировка в ETL — по EGMID DESC) ---
SELECT COUNT(*) AS egisz_messages_in_createdate_window
FROM EGISZ_MESSAGES m
WHERE m.DOCUMENTID IS NOT NULL
  AND TRIM(m.DOCUMENTID) <> ''
  AND m.CREATEDATE >= DATEADD(-30 DAY TO CURRENT_TIMESTAMP);
-- Замените 30 на N.
