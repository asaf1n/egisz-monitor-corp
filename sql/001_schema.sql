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

-- REPLACE VIEW нельзя использовать для смены имён/набора колонок; старые версии отчёта (кириллические AS) ломают повторный apply.
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
