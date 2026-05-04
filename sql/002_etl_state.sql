-- Incremental cursor for Firebird EXCHANGELOG.LOGID (do not use MODIFYDATE as watermark).

CREATE TABLE IF NOT EXISTS etl_state (
    pipeline VARCHAR(64) PRIMARY KEY,
    last_log_id BIGINT NOT NULL DEFAULT 0,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

COMMENT ON TABLE etl_state IS 'High-water marks for corp ETL (Firebird LOGID)';

ALTER TABLE etl_state ADD COLUMN IF NOT EXISTS last_egmid BIGINT NOT NULL DEFAULT 0;
COMMENT ON COLUMN etl_state.last_egmid IS 'Ватермарк max(EGISZ_MESSAGES.EGMID) по строкам журнала, успешно обработанным в прогоне (журнал + outbound). Не задаёт инкрементальную выгрузку stg_egisz_messages_journal (для неё — messages_snapshot_high_egmid). При сбое не двигается.';

ALTER TABLE etl_state ADD COLUMN IF NOT EXISTS messages_snapshot_high_egmid BIGINT NOT NULL DEFAULT 0;
COMMENT ON COLUMN etl_state.messages_snapshot_high_egmid IS 'Ключевой пагинации снимка EGISZ_MESSAGES в Firebird: последний EGMID, до которого дошла упорядоченная выгрузка (EGMID > курсор). Обновляется после успешного sync; при etl.sync_window_days <= 0 сбрасывается вместе с TRUNCATE staging (полный пересъём).';

INSERT INTO etl_state (pipeline, last_log_id, last_egmid)
VALUES ('firebird_exchangelog', 0, 0)
ON CONFLICT (pipeline) DO NOTHING;

-- Снимки MAX(EGMID) / MAX(MODIFYDATE лицензий) из Firebird после успешного ETL (для UI и отбора в прод без FB из conf-ui).
ALTER TABLE etl_state ADD COLUMN IF NOT EXISTS source_max_egmid BIGINT;
ALTER TABLE etl_state ADD COLUMN IF NOT EXISTS source_max_licenses_modifydate TIMESTAMPTZ;
ALTER TABLE etl_state ADD COLUMN IF NOT EXISTS source_peaks_updated_at TIMESTAMPTZ;
COMMENT ON COLUMN etl_state.source_max_egmid IS 'Пик EGMID, записанный после прохода журнала в прогоне (не курсор выгрузки stg_egisz_messages_journal из Firebird). В healthcheck/UI сравнивается с last_egmid как GREATEST(...)';
COMMENT ON COLUMN etl_state.source_max_licenses_modifydate IS 'MAX(MODIFYDATE) из EGISZ_LICENSES — сохранено при последнем успешном sync ETL';
COMMENT ON COLUMN etl_state.source_peaks_updated_at IS 'Когда записали source_max_* при успешном ETL';
