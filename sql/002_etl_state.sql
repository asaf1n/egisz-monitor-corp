-- Incremental cursor for Firebird EXCHANGELOG.LOGID (do not use MODIFYDATE as watermark).

CREATE TABLE IF NOT EXISTS etl_state (
    pipeline VARCHAR(64) PRIMARY KEY,
    last_log_id BIGINT NOT NULL DEFAULT 0,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

COMMENT ON TABLE etl_state IS 'High-water marks for corp ETL (Firebird LOGID)';

INSERT INTO etl_state (pipeline, last_log_id)
VALUES ('firebird_exchangelog', 0)
ON CONFLICT (pipeline) DO NOTHING;
