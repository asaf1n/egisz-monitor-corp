-- Incremental cursor for Firebird EXCHANGELOG.LOGID (do not use MODIFYDATE as watermark).

CREATE TABLE IF NOT EXISTS etl_state (
    pipeline VARCHAR(64) PRIMARY KEY,
    last_log_id BIGINT NOT NULL DEFAULT 0,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

COMMENT ON TABLE etl_state IS 'High-water marks for corp ETL (Firebird LOGID)';

ALTER TABLE etl_state ADD COLUMN IF NOT EXISTS last_egmid BIGINT NOT NULL DEFAULT 0;
COMMENT ON COLUMN etl_state.last_egmid IS 'Курсор инкрементальной выгрузки снимка EGISZ_MESSAGES в Firebird (ключевая пагинация EGMID > last_egmid; отбор строк с непустым DOCUMENTID). Обновляется после успешного sync.';

ALTER TABLE etl_state ADD COLUMN IF NOT EXISTS messages_snapshot_high_egmid BIGINT NOT NULL DEFAULT 0;
COMMENT ON COLUMN etl_state.messages_snapshot_high_egmid IS 'Дублирует last_egmid для совместимости/диагностики (тот же курсор снимка EGISZ_MESSAGES).';

INSERT INTO etl_state (pipeline, last_log_id, last_egmid)
VALUES ('firebird_exchangelog', 0, 0)
ON CONFLICT (pipeline) DO NOTHING;

-- Снимки MAX(EGMID) / MAX(MODIFYDATE лицензий) из Firebird после успешного ETL (для UI и отбора в прод без FB из conf-ui).
ALTER TABLE etl_state ADD COLUMN IF NOT EXISTS source_max_egmid BIGINT;
ALTER TABLE etl_state ADD COLUMN IF NOT EXISTS source_max_licenses_modifydate TIMESTAMPTZ;
ALTER TABLE etl_state ADD COLUMN IF NOT EXISTS source_peaks_updated_at TIMESTAMPTZ;
COMMENT ON COLUMN etl_state.source_max_egmid IS 'Устарело: не используется ETL/UI. Оставлено для совместимости со старыми инсталляциями.';
COMMENT ON COLUMN etl_state.source_max_licenses_modifydate IS 'MAX(MODIFYDATE) из EGISZ_LICENSES — сохранено при последнем успешном sync ETL';
COMMENT ON COLUMN etl_state.source_peaks_updated_at IS 'Когда записали source_max_* при успешном ETL';
