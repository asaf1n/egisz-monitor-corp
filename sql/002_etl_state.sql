-- Incremental cursor for Firebird EXCHANGELOG.LOGID (do not use MODIFYDATE as watermark).

CREATE TABLE IF NOT EXISTS etl_state (
    pipeline VARCHAR(64) PRIMARY KEY,
    last_log_id BIGINT NOT NULL DEFAULT 0,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

COMMENT ON TABLE etl_state IS 'High-water marks for corp ETL (Firebird LOGID)';

ALTER TABLE etl_state ADD COLUMN IF NOT EXISTS last_egmid BIGINT NOT NULL DEFAULT 0;
COMMENT ON COLUMN etl_state.last_egmid IS 'Курсор инкрементальной выгрузки снимка EGISZ_MESSAGES в Firebird (ключевая пагинация EGMID > last_egmid; отбор строк с непустым DOCUMENTID). Обновляется после каждой страницы снимка и в конце успешного sync.';

INSERT INTO etl_state (pipeline, last_log_id, last_egmid)
VALUES ('firebird_exchangelog', 0, 0)
ON CONFLICT (pipeline) DO NOTHING;

-- Кэш MAX(MODIFYDATE) по лицензиям из Firebird после успешного ETL (UI / прод без опроса FB из conf-ui).
ALTER TABLE etl_state ADD COLUMN IF NOT EXISTS source_max_licenses_modifydate TIMESTAMPTZ;
ALTER TABLE etl_state ADD COLUMN IF NOT EXISTS source_peaks_updated_at TIMESTAMPTZ;
COMMENT ON COLUMN etl_state.source_max_licenses_modifydate IS 'MAX(MODIFYDATE) из EGISZ_LICENSES — сохранено при последнем успешном sync ETL';
COMMENT ON COLUMN etl_state.source_peaks_updated_at IS 'Когда записали source_max_licenses_modifydate при успешном ETL';
