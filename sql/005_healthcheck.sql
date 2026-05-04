-- Healthcheck-витрина EGISZ Monitor Corp.
-- Источники: fact_egisz_transactions, dim_clinics, stg_parse_errors, stg_egisz_outbound_documents, etl_state.
-- Запрашиваются Config UI (/api/healthcheck) и дашбордом Metabase 02_service (блок healthcheck).
-- Идемпотентен: применяется в run_sync.apply_reports_schema и в k8s Job egisz-reports-schema-init.

-- Индекс для горячих агрегатов по дате обработки (ETL загрузка / пересчёт healthcheck).
CREATE INDEX IF NOT EXISTS idx_fact_egisz_processed_at ON fact_egisz_transactions (processed_at);
CREATE INDEX IF NOT EXISTS idx_stg_parse_errors_created_at ON stg_parse_errors (created_at);
CREATE INDEX IF NOT EXISTS idx_stg_outbound_sent_at ON stg_egisz_outbound_documents (sent_at);

DROP VIEW IF EXISTS v_health_by_clinic_ui;
DROP VIEW IF EXISTS v_health_signals_ui;
DROP VIEW IF EXISTS v_health_proxy_db_ui;
DROP VIEW IF EXISTS v_health_by_clinic;
DROP VIEW IF EXISTS v_health_signals;
DROP VIEW IF EXISTS v_health_proxy_db;

-- Агрегат по клиникам за последние 24 часа + текущая очередь без ответа.
-- Используется UI вкладкой Healthcheck (топ-3 проблемные клиники) и дашбордом Metabase 11.
CREATE OR REPLACE VIEW v_health_by_clinic AS
WITH facts AS (
    SELECT
        f.jid,
        COUNT(DISTINCT f.relates_to_id) FILTER (WHERE f.processed_at > NOW() - INTERVAL '24 hours') AS facts_24h,
        COUNT(DISTINCT f.relates_to_id) FILTER (WHERE f.status = 'error'   AND f.processed_at > NOW() - INTERVAL '24 hours') AS errors_24h,
        COUNT(DISTINCT f.relates_to_id) FILTER (WHERE f.status = 'unknown' AND f.processed_at > NOW() - INTERVAL '24 hours') AS unknown_24h,
        COUNT(DISTINCT f.relates_to_id) FILTER (WHERE f.status = 'success' AND f.processed_at > NOW() - INTERVAL '24 hours') AS success_24h,
        MAX(f.processed_at) AS last_seen_at
    FROM fact_egisz_transactions f
    WHERE f.jid IS NOT NULL
    GROUP BY f.jid
),
queue AS (
    SELECT jid, COUNT(DISTINCT local_uid_semd)::bigint AS pending_now
    FROM v_rpt_documents_no_response
    WHERE jid IS NOT NULL
    GROUP BY jid
)
SELECT
    f.jid,
    COALESCE(NULLIF(TRIM(dc.jname), ''), 'Клиника JID: ' || f.jid::text) AS clinic_name,
    dc.jinn AS clinic_inn,
    dc.fir_oid AS clinic_mo_oid,
    f.facts_24h,
    f.success_24h,
    f.errors_24h,
    f.unknown_24h,
    ROUND(100.0 * f.errors_24h / NULLIF(f.facts_24h, 0), 2) AS error_rate_24h,
    ROUND(100.0 * f.unknown_24h / NULLIF(f.facts_24h, 0), 2) AS unknown_rate_24h,
    COALESCE(q.pending_now, 0) AS pending_now,
    f.last_seen_at,
    CASE
        WHEN f.facts_24h >= 50 AND f.errors_24h * 1.0 / NULLIF(f.facts_24h, 0) > 0.10 THEN 'red'
        WHEN f.facts_24h >= 20 AND f.errors_24h * 1.0 / NULLIF(f.facts_24h, 0) > 0.05 THEN 'yellow'
        WHEN COALESCE(q.pending_now, 0) >= 50 THEN 'yellow'
        ELSE 'green'
    END AS health_level
FROM facts f
LEFT JOIN dim_clinics dc ON dc.jid = f.jid
LEFT JOIN queue q ON q.jid = f.jid;

COMMENT ON VIEW v_health_by_clinic IS 'Healthcheck по клиникам: объём за 24ч по уникальным relates_to_id (документ/callback), error/unknown rate, очередь без ответа по уникальным local_uid_semd, health_level.';

-- 5 строк-сигналов по всему сервису. Пороги задокументированы в docs/BI_EGISZ_INFOKLINIKA_AUDIT.md §3.3.
-- Каждая строка имеет фиксированный code и текущий level (green/yellow/red).
CREATE OR REPLACE VIEW v_health_signals AS
WITH params AS (
    SELECT
        50::numeric  AS error_min_volume,
        0.10::numeric AS error_red_ratio,
        0.05::numeric AS unknown_yellow_ratio,
        10::numeric  AS parse_burst_per_hour,
        50::numeric  AS queue_red_24h,
        INTERVAL '6 hours' AS cursor_stale_after
),
agg AS (
    SELECT
        (SELECT COUNT(DISTINCT relates_to_id) FROM fact_egisz_transactions WHERE processed_at > NOW() - INTERVAL '24 hours') AS facts_24h,
        (SELECT COUNT(DISTINCT relates_to_id) FROM fact_egisz_transactions WHERE processed_at > NOW() - INTERVAL '24 hours' AND status = 'error') AS errors_24h,
        (SELECT COUNT(DISTINCT relates_to_id) FROM fact_egisz_transactions WHERE processed_at > NOW() - INTERVAL '24 hours' AND status = 'unknown') AS unknown_24h,
        (SELECT COUNT(DISTINCT d.document_group_key) FROM v_stg_parse_errors_by_document d WHERE d.created_at > NOW() - INTERVAL '1 hour') AS parse_errors_1h,
        (SELECT COUNT(DISTINCT r.local_uid_semd) FROM v_rpt_documents_no_response r WHERE r.sent_at < NOW() - INTERVAL '24 hours') AS queue_older_24h,
        (SELECT MAX(updated_at) FROM etl_state) AS etl_last_update
)
SELECT
    'error_rate_high'::text AS code,
    'Доля ошибок РЭМД > порога'::text AS title,
    CASE
        WHEN a.facts_24h >= p.error_min_volume
            AND a.errors_24h * 1.0 / NULLIF(a.facts_24h, 0) > p.error_red_ratio THEN 'red'
        WHEN a.facts_24h >= 20
            AND a.errors_24h * 1.0 / NULLIF(a.facts_24h, 0) > 0.05 THEN 'yellow'
        ELSE 'green'
    END AS level,
    ROUND(100.0 * a.errors_24h / NULLIF(a.facts_24h, 0), 2) AS value,
    '%'::text AS value_unit,
    a.facts_24h AS denominator,
    'За 24 часа: ' || COALESCE(ROUND(100.0 * a.errors_24h / NULLIF(a.facts_24h, 0), 2)::text, '—') ||
        '% ошибок при объёме ' || a.facts_24h::text AS hint
FROM agg a CROSS JOIN params p
UNION ALL
SELECT
    'unknown_high',
    'Высокая доля unknown (нестандартные ответы)',
    CASE
        WHEN a.facts_24h >= 20
            AND a.unknown_24h * 1.0 / NULLIF(a.facts_24h, 0) > p.unknown_yellow_ratio THEN 'yellow'
        ELSE 'green'
    END,
    ROUND(100.0 * a.unknown_24h / NULLIF(a.facts_24h, 0), 2),
    '%',
    a.facts_24h,
    'Если unknown > 5%: проверьте формат XML callback в EXCHANGELOG.MSGTEXT и парсер.'
FROM agg a CROSS JOIN params p
UNION ALL
SELECT
    'parse_errors_burst',
    'Всплеск ошибок парсинга',
    CASE
        WHEN a.parse_errors_1h > p.parse_burst_per_hour THEN 'red'
        WHEN a.parse_errors_1h > 0 THEN 'yellow'
        ELSE 'green'
    END,
    a.parse_errors_1h,
    'docs/hour (distinct document_group_key)',
    NULL,
    'Уникальные документы с ошибкой парсинга за час (по relatesToMessage / localUid / emdrId / id). Подробности: stg_parse_errors, v_stg_parse_errors_by_document.'
FROM agg a CROSS JOIN params p
UNION ALL
SELECT
    'queue_red_24h',
    'Очередь без ответа > 24 часов',
    CASE
        WHEN a.queue_older_24h > p.queue_red_24h THEN 'red'
        WHEN a.queue_older_24h > 10 THEN 'yellow'
        ELSE 'green'
    END,
    a.queue_older_24h,
    'docs',
    NULL,
    'Документы из stg_egisz_outbound_documents без callback (см. дашборд 08).'
FROM agg a CROSS JOIN params p
UNION ALL
SELECT
    'cursor_stale',
    'Курсор ETL не двигался',
    CASE
        WHEN a.etl_last_update IS NULL THEN 'yellow'
        WHEN a.etl_last_update < NOW() - p.cursor_stale_after THEN 'red'
        WHEN a.etl_last_update < NOW() - INTERVAL '2 hours' THEN 'yellow'
        ELSE 'green'
    END,
    EXTRACT(EPOCH FROM (NOW() - COALESCE(a.etl_last_update, NOW())))::numeric,
    'sec_since_update',
    NULL,
    'Если red: проверьте Airflow / Config UI sync; etl_state.updated_at не двигается.'
FROM agg a CROSS JOIN params p;

COMMENT ON VIEW v_health_signals IS 'Пять сигналов healthcheck (error_rate_high, unknown_high, parse_errors_burst, queue_red_24h, cursor_stale) с уровнями green/yellow/red.';

-- Healthcheck прокси-БД: сводка по staging исходящих; last_log_id и курсор EGMID снимка сообщений из etl_state (без MAX(EGMID) по сообщениям в Firebird).
CREATE OR REPLACE VIEW v_health_proxy_db AS
SELECT
    (SELECT COUNT(*)::bigint FROM stg_egisz_outbound_documents) AS stg_outbound_total,
    (SELECT COUNT(*)::bigint FROM stg_egisz_outbound_documents WHERE egmid IS NULL) AS stg_without_egmid,
    (SELECT COUNT(*)::bigint FROM stg_egisz_outbound_documents WHERE jid IS NULL) AS stg_without_jid,
    (SELECT MAX(egmid) FROM stg_egisz_outbound_documents) AS staging_max_egmid,
    (SELECT MAX(sent_at) FROM stg_egisz_outbound_documents) AS staging_max_sent_at,
    (SELECT COUNT(DISTINCT local_uid_semd)::bigint FROM v_rpt_documents_no_response) AS pending_total,
    (SELECT COUNT(DISTINCT local_uid_semd)::bigint FROM v_rpt_documents_no_response WHERE sent_at >= NOW() - INTERVAL '1 hour') AS pending_1h,
    (SELECT COUNT(DISTINCT local_uid_semd)::bigint FROM v_rpt_documents_no_response WHERE sent_at >= NOW() - INTERVAL '24 hours' AND sent_at < NOW() - INTERVAL '1 hour') AS pending_1_24h,
    (SELECT COUNT(DISTINCT local_uid_semd)::bigint FROM v_rpt_documents_no_response WHERE sent_at < NOW() - INTERVAL '24 hours') AS pending_older_24h,
    (SELECT MAX(updated_at) FROM etl_state) AS etl_last_update,
    (SELECT last_log_id FROM etl_state WHERE pipeline = 'firebird_exchangelog') AS etl_last_log_id,
    (SELECT COALESCE(last_egmid, 0)
     FROM etl_state WHERE pipeline = 'firebird_exchangelog' LIMIT 1) AS etl_cursor_egmid;

COMMENT ON VIEW v_health_proxy_db IS 'Сводка по staging исходящих: очередь, max EGMID в staging, etl_state.last_log_id и etl_state.last_egmid как курсор выгрузки снимка EGISZ_MESSAGES.';

-- UI-обёртки с русскими подписями для Metabase native-вопросов.
CREATE OR REPLACE VIEW v_health_by_clinic_ui AS
SELECT
    jid::text                       AS "JID клиники",
    clinic_name                     AS "Наименование клиники",
    clinic_inn                      AS "ИНН клиники",
    clinic_mo_oid                   AS "OID клиники",
    facts_24h                       AS "Документов за 24ч",
    success_24h                     AS "Успехи за 24ч",
    errors_24h                      AS "Ошибки за 24ч",
    unknown_24h                     AS "Unknown за 24ч",
    error_rate_24h                  AS "Доля ошибок, %",
    unknown_rate_24h                AS "Доля unknown, %",
    pending_now                     AS "В очереди (документов)",
    last_seen_at                    AS "Последняя активность",
    health_level                    AS "Уровень здоровья"
FROM v_health_by_clinic;

COMMENT ON VIEW v_health_by_clinic_ui IS 'UI-обёртка v_health_by_clinic с русскими подписями для дашборда 11.';

CREATE OR REPLACE VIEW v_health_signals_ui AS
SELECT
    code        AS "Код сигнала",
    title       AS "Сигнал",
    level       AS "Уровень",
    value       AS "Значение",
    value_unit  AS "Единица",
    denominator AS "База расчёта",
    hint        AS "Что делать"
FROM v_health_signals;

COMMENT ON VIEW v_health_signals_ui IS 'UI-обёртка v_health_signals для дашборда 11 (карточка «Сигналы»).';

CREATE OR REPLACE VIEW v_health_proxy_db_ui AS
SELECT
    stg_outbound_total   AS "Staging: всего строк",
    stg_without_egmid    AS "Без EGMID",
    stg_without_jid      AS "Без JID",
    staging_max_egmid::text    AS "Staging max EGMID",
    staging_max_sent_at  AS "Staging max Sent",
    pending_total        AS "Очередь всего",
    pending_1h           AS "Очередь < 1ч",
    pending_1_24h        AS "Очередь 1–24ч",
    pending_older_24h    AS "Очередь > 24ч",
    etl_last_update      AS "Последний апдейт курсора",
    etl_last_log_id::text      AS "etl_state.last_log_id",
    etl_cursor_egmid::text     AS "etl_state.last_egmid (курсор EGISZ_MESSAGES)"
FROM v_health_proxy_db;

COMMENT ON VIEW v_health_proxy_db_ui IS 'UI-обёртка v_health_proxy_db для дашборда 11 (карточка «Прокси-БД»). Числовые идентификаторы — TEXT для Metabase.';

-- Подписи healthcheck-колонок в общем справочнике.
INSERT INTO dim_column_display_labels (source_object, source_column, display_label_ru) VALUES
    ('v_health_by_clinic', 'jid', 'JID клиники'),
    ('v_health_by_clinic', 'clinic_name', 'Наименование клиники'),
    ('v_health_by_clinic', 'facts_24h', 'Документов за 24ч'),
    ('v_health_by_clinic', 'errors_24h', 'Ошибки за 24ч'),
    ('v_health_by_clinic', 'unknown_24h', 'Unknown за 24ч'),
    ('v_health_by_clinic', 'error_rate_24h', 'Доля ошибок, %'),
    ('v_health_by_clinic', 'pending_now', 'В очереди сейчас'),
    ('v_health_by_clinic', 'health_level', 'Уровень здоровья'),
    ('v_health_signals', 'code', 'Код сигнала'),
    ('v_health_signals', 'level', 'Уровень'),
    ('v_health_signals', 'value', 'Значение'),
    ('v_health_signals', 'hint', 'Что делать'),
    ('v_health_proxy_db', 'stg_outbound_total', 'Staging: всего строк'),
    ('v_health_proxy_db', 'pending_older_24h', 'Очередь > 24ч'),
    ('v_health_proxy_db', 'etl_last_update', 'Последний апдейт курсора'),
    ('v_health_proxy_db', 'etl_cursor_egmid', 'etl_state.last_egmid (курсор EGISZ_MESSAGES)')
ON CONFLICT (source_object, source_column) DO UPDATE SET display_label_ru = EXCLUDED.display_label_ru;
