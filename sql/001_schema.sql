-- EGISZ corporate DWH (Metabase-facing). Полная схема витрины в одном файле.
-- UPSERT key: relates_to_id (SOAP callback). Watermark Firebird: EXCHANGELOG.LOGID → etl_state.
-- Источник полей клиники: Firebird JPERSONS / EGISZ_LICENSES (см. proxy_tables: JINN VARCHAR(12), FIR_OID VARCHAR(255)).

CREATE TABLE IF NOT EXISTS dim_semd_types (
    kind_code VARCHAR(16) PRIMARY KEY,
    kind_name VARCHAR(512) NOT NULL
);

COMMENT ON TABLE dim_semd_types IS 'SEMD type dictionary (NSI codes 2–3 digits, not OID)';

CREATE TABLE IF NOT EXISTS dim_clinics (
    jid BIGINT PRIMARY KEY,
    jname VARCHAR(512),
    mo_uid VARCHAR(256),
    jinn VARCHAR(12),
    fir_oid VARCHAR(255),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

COMMENT ON TABLE dim_clinics IS 'Справочник клиники при ETL: JPERSONS + данные по JID из EGISZ_LICENSES (опционально предзагрузка)';
COMMENT ON COLUMN dim_clinics.jinn IS 'ИНН (JPERSONS.JINN)';
COMMENT ON COLUMN dim_clinics.fir_oid IS 'OID МО (JPERSONS.FIR_OID; как <organization> / EGISZ_LICENSES.MO_UID)';

CREATE TABLE IF NOT EXISTS fact_egisz_transactions (
    relates_to_id VARCHAR(256) PRIMARY KEY,
    local_uid_semd VARCHAR(256),
    jid BIGINT,
    gost_jid_token TEXT,
    org_oid VARCHAR(256),
    kind_code VARCHAR(16),
    status VARCHAR(16) NOT NULL,
    emdr_id VARCHAR(256),
    errors_json JSONB NOT NULL DEFAULT '[]'::jsonb,
    registration_date TIMESTAMPTZ,
    processed_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT chk_fact_status CHECK (status IN ('success', 'error', 'unknown'))
);

COMMENT ON TABLE fact_egisz_transactions IS 'Normalized SOAP registerDocumentResult keyed by relatesToMessage';
COMMENT ON COLUMN fact_egisz_transactions.local_uid_semd IS 'localUid СЭМД: EGISZ_MESSAGES.DOCUMENTID и/или тег localUid в SOAP (EXCHANGELOG.MSGTEXT)';
COMMENT ON COLUMN fact_egisz_transactions.gost_jid_token IS 'Lowercased host segment from gost-<jid>.infoclinica.lan when numeric JID is not resolved';
COMMENT ON COLUMN fact_egisz_transactions.jid IS 'Internal clinic JID: from LOGTEXT URL, EGISZ_LICENSES row, or OID→EGISZ_LICENSES.MO_UID→JID';

CREATE INDEX IF NOT EXISTS idx_fact_egisz_local_uid ON fact_egisz_transactions (local_uid_semd);
CREATE INDEX IF NOT EXISTS idx_fact_egisz_jid ON fact_egisz_transactions (jid);
CREATE INDEX IF NOT EXISTS idx_fact_egisz_org_oid ON fact_egisz_transactions (org_oid);
CREATE INDEX IF NOT EXISTS idx_fact_egisz_kind ON fact_egisz_transactions (kind_code);
CREATE INDEX IF NOT EXISTS idx_fact_egisz_status ON fact_egisz_transactions (status);

CREATE TABLE IF NOT EXISTS stg_parse_errors (
    id BIGSERIAL PRIMARY KEY,
    relates_to_id VARCHAR(256),
    error_code VARCHAR(64) NOT NULL,
    message TEXT NOT NULL,
    log_excerpt TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

COMMENT ON TABLE stg_parse_errors IS 'Rows where MSGTEXT could not yield relates_to_id or XML is unusable';

-- REPLACE VIEW нельзя использовать для смены имён/набора колонок в базовой витрине (ломаются зависимости и ETL).
-- Человекочитаемые имена — в отдельных *_ui представлениях и в dim_column_display_labels.
DROP VIEW IF EXISTS v_rpt_documents_no_response_ui;
DROP VIEW IF EXISTS v_egisz_transactions_enriched_ui;
DROP VIEW IF EXISTS v_rpt_documents_no_response;
DROP VIEW IF EXISTS v_egisz_transactions_enriched;

CREATE OR REPLACE VIEW v_egisz_transactions_enriched AS
SELECT
    f.relates_to_id,
    f.local_uid_semd,
    f.jid,
    f.gost_jid_token,
    f.org_oid,
    f.kind_code,
    dt.kind_name AS kind_name,
    f.status,
    f.emdr_id,
    f.errors_json,
    f.registration_date,
    f.processed_at,
    DATE(COALESCE(f.registration_date, f.processed_at)) AS chart_day,
    COALESCE(NULLIF(TRIM(dc.jname), ''), 'Клиника JID: ' || COALESCE(f.jid::varchar, 'неизвестен')) AS clinic_name,
    dc.jinn AS clinic_inn,
    dc.fir_oid AS clinic_mo_oid
FROM fact_egisz_transactions f
LEFT JOIN dim_semd_types dt ON dt.kind_code = f.kind_code
LEFT JOIN dim_clinics dc ON dc.jid = f.jid;

-- Снимок исходящих сообщений с DOCUMENTID (окно как у ETL). Заполняется пайплайном после загрузки fact.
CREATE TABLE IF NOT EXISTS stg_egisz_outbound_documents (
    document_id VARCHAR(256) PRIMARY KEY,
    sent_at TIMESTAMPTZ,
    reply_to TEXT,
    gost_jid_token TEXT,
    kind_code VARCHAR(16),
    jid BIGINT,
    synced_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

COMMENT ON TABLE stg_egisz_outbound_documents IS 'EGISZ_MESSAGES с непустым DOCUMENTID за окно sync_window_days; для отчёта «Документы без ответа»';
COMMENT ON COLUMN stg_egisz_outbound_documents.sent_at IS 'Дата/время создания строки в EGISZ_MESSAGES (поле CREATEDATE в источнике Firebird; при другом имени колонки поправьте SQL в sql_util.outbound_documents_staging_select)';

CREATE OR REPLACE VIEW v_rpt_documents_no_response AS
SELECT
    o.document_id AS local_uid_semd,
    o.kind_code,
    COALESCE(dt.kind_name, o.kind_code::varchar) AS kind_name,
    o.jid,
    COALESCE(NULLIF(TRIM(dc.jname), ''), 'Клиника JID: ' || COALESCE(o.jid::varchar, 'неизвестен')) AS clinic_name,
    COALESCE(
        CASE
            WHEN o.gost_jid_token IS NOT NULL AND TRIM(o.gost_jid_token) <> ''
                THEN 'gost-' || o.gost_jid_token || '.infoclinica.lan'
        END,
        LEFT(o.reply_to, 512)
    ) AS gost_host,
    o.sent_at
FROM stg_egisz_outbound_documents o
LEFT JOIN dim_semd_types dt ON dt.kind_code = o.kind_code
LEFT JOIN dim_clinics dc ON dc.jid = o.jid
WHERE o.document_id IS NOT NULL
  AND TRIM(o.document_id) <> ''
  AND NOT EXISTS (
    SELECT 1
    FROM fact_egisz_transactions f
    WHERE f.local_uid_semd IS NOT NULL
      AND TRIM(f.local_uid_semd) = TRIM(o.document_id)
  );

COMMENT ON VIEW v_rpt_documents_no_response IS 'Outbound EGISZ_MESSAGES (DOCUMENTID): no fact row with same local_uid_semd. Columns align with v_egisz_transactions_enriched (local_uid_semd, kind_code, kind_name, jid, clinic_name); gost_host = gost-*.infoclinica.lan or reply_to excerpt; sent_at = message row created at source (CREATEDATE).';

-- Сопоставление имён колонок витрины (snake_case) с подписями в отчётах. Синхронизировано с представлениями *_ui.
CREATE TABLE IF NOT EXISTS dim_column_display_labels (
    source_object TEXT NOT NULL,
    source_column TEXT NOT NULL,
    display_label_ru TEXT NOT NULL,
    PRIMARY KEY (source_object, source_column)
);

COMMENT ON TABLE dim_column_display_labels IS 'Имя колонки в представлении/таблице (source_object.source_column) → подпись для Metabase и UI';

INSERT INTO dim_column_display_labels (source_object, source_column, display_label_ru) VALUES
    ('v_egisz_transactions_enriched', 'relates_to_id', 'Связанное сообщение'),
    ('v_egisz_transactions_enriched', 'local_uid_semd', 'localUid СЭМД'),
    ('v_egisz_transactions_enriched', 'jid', 'JID клиники'),
    ('v_egisz_transactions_enriched', 'gost_jid_token', 'Токен gost-хоста'),
    ('v_egisz_transactions_enriched', 'org_oid', 'OID организации'),
    ('v_egisz_transactions_enriched', 'kind_code', 'Код СЭМД'),
    ('v_egisz_transactions_enriched', 'kind_name', 'Наименование СЭМД'),
    ('v_egisz_transactions_enriched', 'status', 'Статус'),
    ('v_egisz_transactions_enriched', 'emdr_id', 'EMDR ID'),
    ('v_egisz_transactions_enriched', 'errors_json', 'Ошибки JSON'),
    ('v_egisz_transactions_enriched', 'registration_date', 'Дата регистрации'),
    ('v_egisz_transactions_enriched', 'processed_at', 'Обработано'),
    ('v_egisz_transactions_enriched', 'chart_day', 'День (тренд)'),
    ('v_egisz_transactions_enriched', 'clinic_name', 'Наименование клиники'),
    ('v_egisz_transactions_enriched', 'clinic_inn', 'ИНН клиники'),
    ('v_egisz_transactions_enriched', 'clinic_mo_oid', 'OID клиники'),
    ('v_rpt_documents_no_response', 'local_uid_semd', 'localUid СЭМД'),
    ('v_rpt_documents_no_response', 'kind_code', 'Код СЭМД'),
    ('v_rpt_documents_no_response', 'kind_name', 'Наименование СЭМД'),
    ('v_rpt_documents_no_response', 'jid', 'JID клиники'),
    ('v_rpt_documents_no_response', 'clinic_name', 'Наименование клиники'),
    ('v_rpt_documents_no_response', 'gost_host', 'Хост клиники (ГОСТ VPN)'),
    ('v_rpt_documents_no_response', 'sent_at', 'Отправлено')
ON CONFLICT (source_object, source_column) DO UPDATE SET display_label_ru = EXCLUDED.display_label_ru;

-- Metabase / отчёты: те же данные, что v_egisz_transactions_enriched, с русскими именами колонок (ResultSet / «Спросить данные»).
CREATE OR REPLACE VIEW v_egisz_transactions_enriched_ui AS
SELECT
    local_uid_semd AS "localUid СЭМД",
    jid::text AS "JID клиники",
    gost_jid_token AS "Токен gost-хоста",
    org_oid AS "OID организации",
    kind_code AS "Код СЭМД",
    kind_name AS "Наименование СЭМД",
    status AS "Статус",
    emdr_id AS "EMDR ID",
    errors_json AS "Ошибки JSON",
    registration_date AS "Дата регистрации",
    processed_at AS "Обработано",
    chart_day AS "День (тренд)",
    clinic_name AS "Наименование клиники",
    clinic_inn AS "ИНН клиники",
    clinic_mo_oid AS "OID клиники",
    relates_to_id AS "Связанное сообщение"
FROM v_egisz_transactions_enriched;

COMMENT ON VIEW v_egisz_transactions_enriched_ui IS 'Обёртка над v_egisz_transactions_enriched с подписями колонок для отчётов; см. dim_column_display_labels. JID клиники — TEXT (идентификатор, не суммируется в Metabase). Колонка «Связанное сообщение» (relates_to_id) — последняя для удобства витрин и Metabase.';

CREATE OR REPLACE VIEW v_rpt_documents_no_response_ui AS
SELECT
    local_uid_semd AS "localUid СЭМД",
    kind_code AS "Код СЭМД",
    kind_name AS "Наименование СЭМД",
    jid::text AS "JID клиники",
    clinic_name AS "Наименование клиники",
    gost_host AS "Хост клиники (ГОСТ VPN)",
    sent_at AS "Отправлено"
FROM v_rpt_documents_no_response;

COMMENT ON VIEW v_rpt_documents_no_response_ui IS 'Обёртка над v_rpt_documents_no_response с подписями колонок для отчётов; см. dim_column_display_labels. JID клиники — TEXT (идентификатор, не суммируется в Metabase).';
