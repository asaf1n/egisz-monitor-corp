-- Диагностические запросы к витрине PostgreSQL (egisz_reports / public).
-- Сопоставляйте с sql/003_diagnostic_counts_firebird.sql и логами: fetched, facts_upserted, cursor_after.

-- Курсор ETL (имя пайплайна как в etl.pipeline_name, по умолчанию firebird_exchangelog)
SELECT pipeline, last_log_id, updated_at
FROM etl_state
ORDER BY pipeline;

-- Количество фактов (документов/callback: один relates_to_id = одна строка)
SELECT COUNT(DISTINCT relates_to_id)::bigint AS fact_documents_total
FROM fact_egisz_transactions;

-- Факты за последние N суток по processed_at (подставьте N из конфига для ориентира; необязательно совпадает с окном Firebird)
SELECT COUNT(DISTINCT relates_to_id)::bigint AS fact_documents_processed_last_30d
FROM fact_egisz_transactions
WHERE processed_at >= NOW() - INTERVAL '30 days';
-- Замените 30 на нужное число дней.

-- Staging исходящих — по одной строке на document_id (PK)
SELECT COUNT(DISTINCT document_id)::bigint AS stg_outbound_documents
FROM stg_egisz_outbound_documents;

-- Ошибки парсинга: уникальные документы (группировка), и сырой объём строк журнала
SELECT COUNT(DISTINCT document_group_key)::bigint AS parse_error_documents
FROM v_stg_parse_errors_by_document;
SELECT COUNT(*)::bigint AS parse_error_staging_rows
FROM stg_parse_errors;
