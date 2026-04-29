-- Диагностические запросы к витрине PostgreSQL (egisz_reports / public).
-- Сопоставляйте с sql/003_diagnostic_counts_firebird.sql и логами: fetched, facts_upserted, cursor_after.

-- Курсор ETL (имя пайплайна как в etl.pipeline_name, по умолчанию firebird_exchangelog)
SELECT pipeline, last_log_id, updated_at
FROM etl_state
ORDER BY pipeline;

-- Количество фактов (накапливаются; не ограничены текущим sync_window_days)
SELECT COUNT(*)::bigint AS fact_rows_total
FROM fact_egisz_transactions;

-- Факты за последние N суток по processed_at (подставьте N из конфига для ориентира; необязательно совпадает с окном Firebird)
SELECT COUNT(*)::bigint AS fact_rows_processed_last_30d
FROM fact_egisz_transactions
WHERE processed_at >= NOW() - INTERVAL '30 days';
-- Замените 30 на нужное число дней.

-- Staging исходящих — полный снимок последнего успешного sync (окно CREATEDATE во Firebird)
SELECT COUNT(*)::bigint AS stg_outbound_rows
FROM stg_egisz_outbound_documents;

-- Ошибки парсинга журнала (объясняют расхождение fetched vs facts)
SELECT COUNT(*)::bigint AS parse_error_rows
FROM stg_parse_errors;
