-- EGISZ corporate DWH (Metabase-facing). UPSERT key: relates_to_id (SOAP callback).
-- Do not use EXCHANGELOG.MODIFYDATE as extraction watermark (source overwrites rows).

CREATE TABLE IF NOT EXISTS dim_semd_types (
    kind_code VARCHAR(16) PRIMARY KEY,
    kind_name VARCHAR(512) NOT NULL
);

COMMENT ON TABLE dim_semd_types IS 'SEMD type dictionary (NSI codes 2–3 digits, not OID)';

CREATE TABLE IF NOT EXISTS dim_clinics (
    jid BIGINT PRIMARY KEY,
    jname VARCHAR(512),
    mo_uid VARCHAR(256),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

COMMENT ON TABLE dim_clinics IS 'Optional clinic dimension: JID from EGISZ_LICENSES, name from JPERSONS';

CREATE TABLE IF NOT EXISTS fact_egisz_transactions (
    relates_to_id VARCHAR(256) PRIMARY KEY,
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
COMMENT ON COLUMN fact_egisz_transactions.gost_jid_token IS 'Lowercased host segment from gost-<jid>.infoclinica.lan when numeric JID is not resolved';
COMMENT ON COLUMN fact_egisz_transactions.jid IS 'Resolved internal clinic id when URL yields digits or OID→license→JID';

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

COMMENT ON TABLE stg_parse_errors IS 'Rows where LOGTEXT could not yield relates_to_id or XML is unusable';

CREATE OR REPLACE VIEW v_egisz_transactions_enriched AS
SELECT
    f.relates_to_id,
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
    dc.jname AS clinic_name
FROM fact_egisz_transactions f
LEFT JOIN dim_semd_types dt ON dt.kind_code = f.kind_code
LEFT JOIN dim_clinics dc ON dc.jid = f.jid;
