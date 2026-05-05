-- EGISZ corporate DWH (Metabase-facing). Полная схема витрины в одном файле.
-- UPSERT key: relates_to_id (SOAP callback). Watermark Firebird: EXCHANGELOG.LOGID → etl_state.
-- MSGID — идентификатор сообщения в обмене; EXCHANGELOG.MSGID ссылается на EGISZ_MESSAGES.MSGID; EGMID — суррогатный ключ строки EGISZ_MESSAGES (РЭМД).
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
    semd_creation_at TIMESTAMPTZ,
    processed_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT chk_fact_status CHECK (status IN ('success', 'error', 'unknown'))
);

COMMENT ON TABLE fact_egisz_transactions IS 'Normalized SOAP registerDocumentResult keyed by relatesToMessage';
COMMENT ON COLUMN fact_egisz_transactions.local_uid_semd IS 'localUid СЭМД: EGISZ_MESSAGES.DOCUMENTID и/или тег localUid в SOAP (EXCHANGELOG.MSGTEXT)';
COMMENT ON COLUMN fact_egisz_transactions.gost_jid_token IS 'Нецифровой сегмент gost-* из LOGTEXT/REPLYTO (если числовой JID извлечён — обычно NULL); исторически также дублирует отображение токена';

ALTER TABLE fact_egisz_transactions ADD COLUMN IF NOT EXISTS jid_from_license BIGINT;
ALTER TABLE fact_egisz_transactions ADD COLUMN IF NOT EXISTS jid_from_gost_log BIGINT;
ALTER TABLE fact_egisz_transactions ADD COLUMN IF NOT EXISTS jid_from_gost_reply BIGINT;
ALTER TABLE fact_egisz_transactions ADD COLUMN IF NOT EXISTS gost_token_logtext TEXT;
ALTER TABLE fact_egisz_transactions ADD COLUMN IF NOT EXISTS gost_token_replyto TEXT;
ALTER TABLE fact_egisz_transactions ADD COLUMN IF NOT EXISTS jid_sources_mismatch BOOLEAN NOT NULL DEFAULT false;

COMMENT ON COLUMN fact_egisz_transactions.jid_from_license IS 'JID из строки EGISZ_LICENSES по REPLYTO (если журнал сопоставлен с EGISZ_MESSAGES)';
COMMENT ON COLUMN fact_egisz_transactions.jid_from_gost_log IS 'Числовой JID из первого gost-* в EXCHANGELOG.LOGTEXT';
COMMENT ON COLUMN fact_egisz_transactions.jid_from_gost_reply IS 'Числовой JID из первого gost-* в EGISZ_MESSAGES.REPLYTO';
COMMENT ON COLUMN fact_egisz_transactions.gost_token_logtext IS 'Сегмент токена gost-* в LOGTEXT (включая нецифровой)';
COMMENT ON COLUMN fact_egisz_transactions.gost_token_replyto IS 'Сегмент токена gost-* в REPLYTO (включая нецифровой)';
COMMENT ON COLUMN fact_egisz_transactions.jid_sources_mismatch IS 'Несовпадение JID между лицензией и gost в LOGTEXT/REPLYTO (или разные токены gost)';
COMMENT ON COLUMN fact_egisz_transactions.jid IS 'Итоговый JID в витрине: resolve (gost LOGTEXT/REPLYTO, EGISZ_LICENSES, OID); имя клиники — dim_clinics/JPERSONS по этому JID';

CREATE INDEX IF NOT EXISTS idx_fact_jid_sources_mismatch ON fact_egisz_transactions (jid_sources_mismatch) WHERE jid_sources_mismatch;

CREATE INDEX IF NOT EXISTS idx_fact_egisz_local_uid ON fact_egisz_transactions (local_uid_semd);
CREATE INDEX IF NOT EXISTS idx_fact_egisz_jid ON fact_egisz_transactions (jid);
CREATE INDEX IF NOT EXISTS idx_fact_egisz_org_oid ON fact_egisz_transactions (org_oid);
CREATE INDEX IF NOT EXISTS idx_fact_egisz_kind ON fact_egisz_transactions (kind_code);
CREATE INDEX IF NOT EXISTS idx_fact_egisz_status ON fact_egisz_transactions (status);

ALTER TABLE fact_egisz_transactions ADD COLUMN IF NOT EXISTS semd_creation_at TIMESTAMPTZ;

COMMENT ON COLUMN fact_egisz_transactions.registration_date IS 'Дата/время регистрации в ЕГИСЗ РЭМД: тег registrationDateTime (предпочтительно) или registrationDate в XML из EXCHANGELOG.MSGTEXT';
COMMENT ON COLUMN fact_egisz_transactions.semd_creation_at IS 'Дата/время создания СЭМД: тег creationDateTime в XML из EXCHANGELOG.MSGTEXT';
COMMENT ON COLUMN fact_egisz_transactions.processed_at IS 'Обработано IPS: CREATEDATE строки EGISZ_MESSAGES (по MSGID журнала); если сообщение не найдено в снимке — CREATEDATE строки EXCHANGELOG; иначе время загрузки ETL';

ALTER TABLE fact_egisz_transactions ADD COLUMN IF NOT EXISTS exchangelog_log_id BIGINT;
ALTER TABLE fact_egisz_transactions ADD COLUMN IF NOT EXISTS egisz_messages_egmid BIGINT;

COMMENT ON COLUMN fact_egisz_transactions.exchangelog_log_id IS 'EXCHANGELOG.LOGID строки журнала-источника (как водяной знак ETL, но на уровне факта для отчётов)';
COMMENT ON COLUMN fact_egisz_transactions.egisz_messages_egmid IS 'EGISZ_MESSAGES.EGMID — суррогатный ключ записи сообщения (фиксация исходящего при отправке в РЭМД); подставляется из stg_egisz_messages_journal по EXCHANGELOG.MSGID (= MSGID сообщения)';

CREATE INDEX IF NOT EXISTS idx_fact_egisz_exchangelog_log_id ON fact_egisz_transactions (exchangelog_log_id);
CREATE INDEX IF NOT EXISTS idx_fact_egisz_egisz_messages_egmid ON fact_egisz_transactions (egisz_messages_egmid);

ALTER TABLE fact_egisz_transactions ADD COLUMN IF NOT EXISTS journal_msgid VARCHAR(256);

COMMENT ON COLUMN fact_egisz_transactions.journal_msgid IS 'EXCHANGELOG.MSGID (= EGISZ_MESSAGES.MSGID) строки журнала, по которой построен факт; для поиска в Metabase по идентификатору обмена';

CREATE INDEX IF NOT EXISTS idx_fact_journal_msgid ON fact_egisz_transactions (journal_msgid) WHERE journal_msgid IS NOT NULL;

CREATE TABLE IF NOT EXISTS stg_parse_errors (
    id BIGSERIAL PRIMARY KEY,
    relates_to_id VARCHAR(256),
    error_code VARCHAR(64) NOT NULL,
    message TEXT NOT NULL,
    log_excerpt TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

ALTER TABLE stg_parse_errors ADD COLUMN IF NOT EXISTS exchangelog_log_id BIGINT;
ALTER TABLE stg_parse_errors ADD COLUMN IF NOT EXISTS egisz_messages_egmid BIGINT;
ALTER TABLE stg_parse_errors ADD COLUMN IF NOT EXISTS journal_msgid VARCHAR(256);
ALTER TABLE stg_parse_errors ADD COLUMN IF NOT EXISTS relates_to_hint VARCHAR(512);
ALTER TABLE stg_parse_errors ADD COLUMN IF NOT EXISTS local_uid_hint VARCHAR(512);
ALTER TABLE stg_parse_errors ADD COLUMN IF NOT EXISTS emdr_id_hint VARCHAR(512);

COMMENT ON TABLE stg_parse_errors IS 'Rows where MSGTEXT could not yield relates_to_id or XML is unusable; see relates_to_hint / local_uid_hint / emdr_id_hint and EXCHANGELOG keys for document-level reporting';
COMMENT ON COLUMN stg_parse_errors.exchangelog_log_id IS 'EXCHANGELOG.LOGID — исходная строка прокси-журнала (трассировка)';
COMMENT ON COLUMN stg_parse_errors.egisz_messages_egmid IS 'EGISZ_MESSAGES.EGMID — идентификатор записи сообщения (трассировка)';
COMMENT ON COLUMN stg_parse_errors.journal_msgid IS 'EXCHANGELOG.MSGID — идентификатор сообщения в контуре обмена (трассировка к EGISZ_MESSAGES.MSGID)';
COMMENT ON COLUMN stg_parse_errors.relates_to_hint IS 'Регексп по тегу relatesToMessage в сыром MSGTEXT, если факт не построен';
COMMENT ON COLUMN stg_parse_errors.local_uid_hint IS 'Регексп по localUid в сыром MSGTEXT (экземпляр СЭМД в МИС)';
COMMENT ON COLUMN stg_parse_errors.emdr_id_hint IS 'Регексп по emdrId в сыром MSGTEXT (рег. номер в РЭМД, если уже фигурирует в теле)';

-- Ключ для агрегации «один документ — одна ошибка» в Metabase/healthcheck: coalesce идентификаторов, иначе уникальный id строки.
CREATE OR REPLACE VIEW v_stg_parse_errors_by_document AS
SELECT
    s.*,
    COALESCE(
        NULLIF(TRIM(s.relates_to_hint), ''),
        NULLIF(TRIM(s.local_uid_hint), ''),
        NULLIF(TRIM(s.emdr_id_hint), ''),
        NULLIF(TRIM(s.relates_to_id), ''),
        'id:' || s.id::text
    ) AS document_group_key
FROM stg_parse_errors s;

COMMENT ON VIEW v_stg_parse_errors_by_document IS 'stg_parse_errors + document_group_key: агрегация по уникальным документам (document_group_key), а не по числу строк EXCHANGELOG';

-- Агрегированная «человекочитаемая» сводка по errors_json: одна строка на факт, без перезаписи уже ясных сообщений (ГИП и т.п.).
-- Разбор нескольких блоков Schematron в одном message: разделитель внутри элемента — " — ".

CREATE OR REPLACE FUNCTION egisz_friendly_schematron_chunk(p_chunk text)
RETURNS text
LANGUAGE plpgsql
IMMUTABLE
AS $c$
DECLARE
  t text;
  rid text;
BEGIN
  t := trim(p_chunk);
  IF t = '' THEN
    RETURN NULL;
  END IF;

  rid := (regexp_match(t, 'У[0-9]+(?:[.-][0-9A-Za-z.]+)*'))[1];

  IF t ~* 'address:Type' AND t ~* 'patientRole' AND t ~* 'addr' THEN
    RETURN 'Не указан адрес пациента';
  END IF;

  IF t ~* 'identity:IssueDate' OR (t ~* 'IdentityDoc' AND t ~* 'IssueDate') THEN
    RETURN 'ДУЛ: не заполнена дата выдачи (атрибут @value или реквизит)';
  END IF;

  IF t ~* 'IdentityCardType' THEN
    RETURN 'ДУЛ: проверьте тип документа / реквизиты удостоверения';
  END IF;

  IF t ~* 'patientRole' AND t ~* 'addr' AND t ~* 'равным' THEN
    RETURN 'Проверьте адрес пациента (код типа адреса регистрации)';
  END IF;

  IF rid IS NOT NULL THEN
    RETURN 'Правило ' || rid || ': ' || left(t, 200) || CASE WHEN length(t) > 200 THEN '…' ELSE '' END;
  END IF;

  RETURN 'Проверка схемы: ' || left(t, 220) || CASE WHEN length(t) > 220 THEN '…' ELSE '' END;
END;
$c$;

CREATE OR REPLACE FUNCTION egisz_friendly_error_item(p_code text, p_message text)
RETURNS text
LANGUAGE plpgsql
IMMUTABLE
AS $e$
DECLARE
  m text;
  parts text[];
  chunk text;
  out_parts text[] := ARRAY[]::text[];
  deduped text[] := ARRAY[]::text[];
  p text;
  n int;
  i int;
BEGIN
  m := trim(COALESCE(p_message, ''));
  IF m = '' THEN
    IF nullif(trim(COALESCE(p_code, '')), '') IS NOT NULL THEN
      RETURN 'Код: ' || p_code;
    END IF;
    RETURN NULL;
  END IF;

  -- Уже сформулировано в терминах бизнес-логики: не трогаем
  IF m ~* 'не соответствует данным гип' THEN
    RETURN m;
  END IF;
  IF m ~* 'пациент найден по локальному' THEN
    RETURN m;
  END IF;
  -- Не Schematron-каскад: оставляем как есть (в т.ч. cvc-, справочники, короткие ответы)
  IF m !~* 'schematron' AND m !~* 'схематрона' THEN
    RETURN m;
  END IF;

  parts := string_to_array(
    regexp_replace(
      m,
      'Ошибка валидации (schematron|схематрона)\s*:\s*',
      E'\x1E',
      'gi'
    ),
    E'\x1E'
  );
  n := coalesce(array_length(parts, 1), 0);
  FOR i IN 1..n LOOP
    chunk := nullif(trim(parts[i]), '');
    IF chunk IS NULL THEN
      CONTINUE;
    END IF;
    out_parts := array_append(out_parts, egisz_friendly_schematron_chunk(chunk));
  END LOOP;

  IF coalesce(array_length(out_parts, 1), 0) = 0 THEN
    RETURN m;
  END IF;

  FOREACH p IN ARRAY out_parts
  LOOP
    IF p IS NULL OR p = '' THEN
      CONTINUE;
    END IF;
    IF p = ANY (deduped) THEN
      CONTINUE;
    END IF;
    deduped := array_append(deduped, p);
  END LOOP;

  RETURN array_to_string(deduped, ' — ');
END;
$e$;

COMMENT ON FUNCTION egisz_friendly_error_item IS 'Одна строка-подсказка по code+message; Schematron с несколькими блоками склеивает " — "; исходный текст, если нет schematron/схематрона.';

CREATE OR REPLACE FUNCTION egisz_friendly_errors_row(p_errors jsonb)
RETURNS text
LANGUAGE sql
IMMUTABLE
AS $r$
  SELECT NULLIF(
    string_agg(egisz_friendly_error_item(e->>'code', e->>'message'), ' · ' ORDER BY o),
    ''
  )
  FROM jsonb_array_elements(COALESCE(p_errors, '[]'::jsonb)) WITH ORDINALITY AS t(e, o);
$r$;

COMMENT ON FUNCTION egisz_friendly_errors_row IS 'Сводка по массиву errors_json: элементы разделены " · " (средняя точка).';

-- REPLACE VIEW нельзя использовать для смены имён/набора колонок в базовой витрине (ломаются зависимости и ETL).
-- Человекочитаемые имена — в отдельных *_ui представлениях и в dim_column_display_labels.
-- Сначала дропаем потенциальные зависимости от healthcheck-витрины (sql/005_healthcheck.sql),
-- иначе DROP базовых v_rpt_* / v_egisz_* падает из-за foreign view dependencies.
DROP VIEW IF EXISTS v_health_by_clinic_ui;
DROP VIEW IF EXISTS v_health_signals_ui;
DROP VIEW IF EXISTS v_health_proxy_db_ui;
DROP VIEW IF EXISTS v_health_by_clinic;
DROP VIEW IF EXISTS v_health_signals;
DROP VIEW IF EXISTS v_health_proxy_db;
DROP VIEW IF EXISTS v_rpt_semd_archive_ui;
DROP VIEW IF EXISTS v_rpt_documents_no_response_ui;
DROP VIEW IF EXISTS v_egisz_transactions_enriched_ui;
DROP VIEW IF EXISTS v_rpt_documents_no_response;
DROP VIEW IF EXISTS v_egisz_transactions_enriched;

CREATE OR REPLACE VIEW v_egisz_transactions_enriched AS
SELECT
    f.relates_to_id,
    f.exchangelog_log_id,
    f.egisz_messages_egmid,
    f.local_uid_semd,
    f.jid,
    f.gost_jid_token,
    COALESCE(
        CASE
            WHEN f.gost_token_logtext IS NOT NULL AND TRIM(f.gost_token_logtext) <> ''
                THEN 'gost-' || f.gost_token_logtext || '.infoclinica.lan'
        END,
        CASE
            WHEN f.gost_token_replyto IS NOT NULL AND TRIM(f.gost_token_replyto) <> ''
                THEN 'gost-' || f.gost_token_replyto || '.infoclinica.lan'
        END,
        CASE
            WHEN f.gost_jid_token IS NOT NULL AND TRIM(f.gost_jid_token) <> ''
                THEN 'gost-' || f.gost_jid_token || '.infoclinica.lan'
        END
    ) AS gost_host,
    f.org_oid,
    f.kind_code,
    dt.kind_name AS kind_name,
    f.status,
    f.emdr_id,
    f.errors_json,
    egisz_friendly_errors_row(f.errors_json) AS errors_friendly,
    f.registration_date,
    f.semd_creation_at,
    f.processed_at,
    DATE(COALESCE(f.registration_date, f.processed_at)) AS chart_day,
    COALESCE(NULLIF(TRIM(dc.jname), ''), 'Клиника JID: ' || COALESCE(f.jid::varchar, 'неизвестен')) AS clinic_name,
    dc.jinn AS clinic_inn,
    dc.fir_oid AS clinic_mo_oid,
    f.jid_from_license,
    f.jid_from_gost_log,
    f.jid_from_gost_reply,
    f.gost_token_logtext,
    f.gost_token_replyto,
    f.jid_sources_mismatch,
    f.journal_msgid
FROM fact_egisz_transactions f
LEFT JOIN dim_semd_types dt ON dt.kind_code = f.kind_code
LEFT JOIN dim_clinics dc ON dc.jid = f.jid;

-- Сырой снимок JPERSONS за один прогон ETL (полная выгрузка с Firebird); сшивка с лицензиями — UPDATE в SQL.
CREATE TABLE IF NOT EXISTS stg_jpersons_import (
    jid BIGINT NOT NULL PRIMARY KEY,
    jname VARCHAR(512),
    jinn VARCHAR(12),
    fir_oid VARCHAR(255),
    loaded_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

COMMENT ON TABLE stg_jpersons_import IS 'Полная выгрузка JPERSONS с Firebird; JOIN к stg_egisz_licenses_import по JID в PostgreSQL';

CREATE INDEX IF NOT EXISTS idx_stg_jp_import_jid ON stg_jpersons_import (jid);

-- Сырой снимок EGISZ_LICENSES за один прогон ETL (полная выгрузка с Firebird; без JOIN в Firebird).
-- Поля jname/jinn/fir_oid заполняются из stg_jpersons_import в PostgreSQL; upsert в dim_clinics — merge_dim_clinics_from_license_staging.
CREATE TABLE IF NOT EXISTS stg_egisz_licenses_import (
    fb_id BIGINT,
    jid BIGINT,
    mo_uid VARCHAR(256),
    mo_domen VARCHAR(512),
    modifydate TIMESTAMPTZ,
    egisz_licenses_kind TEXT,
    jname VARCHAR(512),
    jinn VARCHAR(12),
    fir_oid VARCHAR(255),
    loaded_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

COMMENT ON TABLE stg_egisz_licenses_import IS 'Полная выгрузка EGISZ_LICENSES с Firebird; JNAME/JINN/FIR_OID — из stg_jpersons_import (UPDATE в SQL); merge в dim_clinics в PostgreSQL';

CREATE INDEX IF NOT EXISTS idx_stg_lic_import_jid ON stg_egisz_licenses_import (jid);

-- Снимок EGISZ_MESSAGES за прогон (окно CREATEDATE как у журнала; только строки с непустым DOCUMENTID): сопоставление с EXCHANGELOG по MSGID только в PostgreSQL.
CREATE TABLE IF NOT EXISTS stg_egisz_messages_journal (
    msgid VARCHAR(512) NOT NULL PRIMARY KEY,
    egmid BIGINT,
    replyto TEXT,
    documentid TEXT,
    msg_created_at TIMESTAMPTZ,
    loaded_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

COMMENT ON TABLE stg_egisz_messages_journal IS 'Зеркало EGISZ_MESSAGES (непустой DOCUMENTID, окно CREATEDATE как у журнала при sync_window_days > 0): инкрементальная выгрузка по EGMID с водяным знаком etl_state.last_egmid; при sync_window_days <= 0 — TRUNCATE и полный пересъём без окна по дате; догрузка по MSGID из пакетов журнала. Сопоставление EXCHANGELOG.MSGID = MSGID — в PostgreSQL.';

CREATE INDEX IF NOT EXISTS idx_stg_em_journal_msgid ON stg_egisz_messages_journal (msgid);

-- Снимок исходящих сообщений с DOCUMENTID (инкремент по EGMID как у ETL). Заполняется пайплайном после загрузки fact.
CREATE TABLE IF NOT EXISTS stg_egisz_outbound_documents (
    document_id VARCHAR(256) PRIMARY KEY,
    sent_at TIMESTAMPTZ,
    reply_to TEXT,
    gost_jid_token TEXT,
    kind_code VARCHAR(16),
    jid BIGINT,
    egmid BIGINT,
    synced_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

ALTER TABLE stg_egisz_outbound_documents ADD COLUMN IF NOT EXISTS egmid BIGINT;

COMMENT ON TABLE stg_egisz_outbound_documents IS 'EGISZ_MESSAGES с непустым DOCUMENTID в окне CREATEDATE (sync_window_days, как у журнала); для отчёта «Документы без ответа»';
COMMENT ON COLUMN stg_egisz_outbound_documents.egmid IS 'EGISZ_MESSAGES.EGMID — идентификатор записи сообщения при последнем снимке staging';
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
  )
UNION ALL
SELECT
    TRIM(j.documentid) AS local_uid_semd,
    NULL::varchar(16) AS kind_code,
    NULL::varchar AS kind_name,
    NULL::bigint AS jid,
    'Клиника JID: неизвестен'::varchar(512) AS clinic_name,
    LEFT(TRIM(j.replyto), 512) AS gost_host,
    j.msg_created_at AS sent_at
FROM stg_egisz_messages_journal j
WHERE j.documentid IS NOT NULL
  AND TRIM(j.documentid) <> ''
  AND NOT EXISTS (
    SELECT 1
    FROM fact_egisz_transactions f
    WHERE f.local_uid_semd IS NOT NULL
      AND TRIM(f.local_uid_semd) = TRIM(j.documentid)
  )
  AND NOT EXISTS (
    SELECT 1
    FROM stg_egisz_outbound_documents o
    WHERE o.document_id IS NOT NULL
      AND TRIM(o.document_id) = TRIM(j.documentid)
  );

COMMENT ON VIEW v_rpt_documents_no_response IS 'Документы без колбэка в fact: строки из stg_egisz_outbound_documents (снимок Firebird) минус факты; UNION ALL — строки только из stg_egisz_messages_journal (ещё не попали в outbound-снимок). gost_host / клиника для журнала — упрощённо по replyto.';

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
    ('v_egisz_transactions_enriched', 'exchangelog_log_id', 'LOGID журнала EXCHANGELOG'),
    ('v_egisz_transactions_enriched', 'egisz_messages_egmid', 'EGISZ_MESSAGES.EGMID (ключ записи сообщения, РЭМД)'),
    ('v_egisz_transactions_enriched', 'local_uid_semd', 'localUid СЭМД'),
    ('v_egisz_transactions_enriched', 'jid', 'JID клиники'),
    ('v_egisz_transactions_enriched', 'gost_jid_token', 'Токен gost (нецифр., для отображения)'),
    ('v_egisz_transactions_enriched', 'gost_host', 'Хост клиники (VPN ГОСТ)'),
    ('v_egisz_transactions_enriched', 'org_oid', 'OID организации'),
    ('v_egisz_transactions_enriched', 'kind_code', 'Код СЭМД'),
    ('v_egisz_transactions_enriched', 'kind_name', 'Наименование СЭМД'),
    ('v_egisz_transactions_enriched', 'status', 'Статус'),
    ('v_egisz_transactions_enriched', 'emdr_id', 'Рег. номер РЭМД (emdrid)'),
    ('v_egisz_transactions_enriched', 'errors_json', 'Ошибки JSON'),
    (
        'v_egisz_transactions_enriched',
        'errors_friendly',
        'Сводка ошибок: одна строка; внутри одного сообщения Schematron блоки — «—», несколько item в JSON — «·»'
    ),
    (
        'v_egisz_transactions_enriched',
        'registration_date',
        'Зарегистрирован в ЕГИСЗ РЭМД'
    ),
    ('v_egisz_transactions_enriched', 'semd_creation_at', 'Создание СЭМД'),
    ('v_egisz_transactions_enriched', 'processed_at', 'Обработано IPS'),
    ('v_egisz_transactions_enriched', 'chart_day', 'День (тренд)'),
    ('v_egisz_transactions_enriched', 'clinic_name', 'Наименование клиники'),
    ('v_egisz_transactions_enriched', 'clinic_inn', 'ИНН клиники'),
    ('v_egisz_transactions_enriched', 'clinic_mo_oid', 'OID клиники'),
    ('v_egisz_transactions_enriched', 'jid_from_license', 'JID (EGISZ_LICENSES)'),
    ('v_egisz_transactions_enriched', 'jid_from_gost_log', 'JID из gost в LOGTEXT'),
    ('v_egisz_transactions_enriched', 'jid_from_gost_reply', 'JID из gost в REPLYTO'),
    ('v_egisz_transactions_enriched', 'gost_token_logtext', 'Сегмент токена gost (LOGTEXT)'),
    ('v_egisz_transactions_enriched', 'gost_token_replyto', 'Сегмент токена gost (REPLYTO)'),
    ('v_egisz_transactions_enriched', 'jid_sources_mismatch', 'Расхождение источников JID'),
    ('v_egisz_transactions_enriched', 'journal_msgid', 'MSGID обмена (EXCHANGELOG)'),
    ('v_rpt_documents_no_response', 'local_uid_semd', 'localUid СЭМД'),
    ('v_rpt_documents_no_response', 'kind_code', 'Код СЭМД'),
    ('v_rpt_documents_no_response', 'kind_name', 'Наименование СЭМД'),
    ('v_rpt_documents_no_response', 'jid', 'JID клиники'),
    ('v_rpt_documents_no_response', 'clinic_name', 'Наименование клиники'),
    ('v_rpt_documents_no_response', 'gost_host', 'Хост клиники (VPN ГОСТ)'),
    ('v_rpt_documents_no_response', 'sent_at', 'Отправлено')
ON CONFLICT (source_object, source_column) DO UPDATE SET display_label_ru = EXCLUDED.display_label_ru;

-- Metabase / отчёты: те же данные, что v_egisz_transactions_enriched, с русскими именами колонок (ResultSet / «Спросить данные»).
CREATE OR REPLACE VIEW v_egisz_transactions_enriched_ui AS
SELECT
    local_uid_semd AS "localUid СЭМД",
    exchangelog_log_id::text AS "LOGID журнала EXCHANGELOG",
    egisz_messages_egmid::text AS "EGISZ_MESSAGES.EGMID (ключ записи, РЭМД)",
    jid::text AS "JID клиники",
    gost_host AS "Хост клиники (VPN ГОСТ)",
    org_oid AS "OID организации",
    kind_code::text AS "Код СЭМД",
    kind_name AS "Наименование СЭМД",
    status AS "Статус",
    emdr_id AS "Рег. номер РЭМД (emdrid)",
    errors_json AS "Ошибки JSON",
    errors_friendly AS "Сводка ошибок",
    registration_date AS "Зарегистрирован в ЕГИСЗ РЭМД",
    semd_creation_at AS "Создание СЭМД",
    processed_at AS "Обработано IPS",
    chart_day AS "День (тренд)",
    clinic_name AS "Наименование клиники",
    clinic_inn AS "ИНН клиники",
    clinic_mo_oid AS "OID клиники",
    jid_from_license::text AS "JID (EGISZ_LICENSES)",
    jid_from_gost_log::text AS "JID из gost в LOGTEXT",
    jid_from_gost_reply::text AS "JID из gost в REPLYTO",
    gost_token_logtext AS "Токен gost (LOGTEXT)",
    gost_token_replyto AS "Токен gost (REPLYTO)",
    jid_sources_mismatch AS "Расхождение источников JID",
    journal_msgid AS "MSGID обмена (EXCHANGELOG)",
    relates_to_id AS "Связанное сообщение"
FROM v_egisz_transactions_enriched;

COMMENT ON VIEW v_egisz_transactions_enriched_ui IS 'Обёртка над v_egisz_transactions_enriched с подписями колонок для отчётов; см. dim_column_display_labels. JID клиники, Код СЭМД, LOGID/EGMID — TEXT (идентификаторы: без разделителей тысяч и суммирования в Metabase). «Сводка ошибок» — errors_friendly: агрегация подсказок по errors_json, исходные «Ошибки JSON» не меняются. Колонка «Связанное сообщение» (relates_to_id) — последняя для удобства витрин и Metabase.';

-- Архив СЭМД: колбэки (fact) + исходящие без ответа из staging (ожидание колбэка по EXCHANGELOG).
CREATE OR REPLACE VIEW v_rpt_semd_archive_ui AS
SELECT
    e.processed_at AS "Дата обработки",
    e.jid::text AS "JID",
    NULLIF(
        TRIM(
            CONCAT_WS(
                ' — ',
                NULLIF(TRIM(e.kind_code::text), ''),
                NULLIF(TRIM(e.kind_name), '')
            )
        ),
        ''
    ) AS "Тип и наименование СЭМД",
    e.kind_code::text AS "Код СЭМД",
    e.kind_name AS "Наименование СЭМД",
    e.local_uid_semd AS "localUid СЭМД",
    e.emdr_id AS "Рег. номер РЭМД",
    e.status::text AS "Статус",
    e.errors_friendly AS "Сводка ошибок",
    e.errors_json AS "Ошибки JSON",
    e.registration_date AS "Зарегистрирован в ЕГИСЗ РЭМД",
    e.semd_creation_at AS "Создание СЭМД",
    e.chart_day AS "День (тренд)",
    e.clinic_name AS "Наименование клиники",
    e.clinic_inn AS "ИНН клиники",
    e.clinic_mo_oid AS "OID клиники",
    e.exchangelog_log_id::text AS "LOGID журнала EXCHANGELOG",
    e.egisz_messages_egmid::text AS "EGISZ_MESSAGES.EGMID (ключ записи, РЭМД)",
    e.gost_host AS "Хост клиники (VPN ГОСТ)",
    e.org_oid AS "OID организации",
    e.gost_jid_token AS "Токен gost (нецифр., для отображения)",
    e.jid_from_license::text AS "JID (EGISZ_LICENSES)",
    e.jid_from_gost_log::text AS "JID из gost в LOGTEXT",
    e.jid_from_gost_reply::text AS "JID из gost в REPLYTO",
    e.gost_token_logtext AS "Токен gost (LOGTEXT)",
    e.gost_token_replyto AS "Токен gost (REPLYTO)",
    e.jid_sources_mismatch AS "Расхождение источников JID",
    e.journal_msgid::text AS "MSGID обмена (EXCHANGELOG)",
    e.relates_to_id AS "Связанное сообщение"
FROM v_egisz_transactions_enriched e
UNION ALL
SELECT
    o.sent_at AS "Дата обработки",
    o.jid::text AS "JID",
    NULLIF(
        TRIM(
            CONCAT_WS(
                ' — ',
                NULLIF(TRIM(o.kind_code::text), ''),
                NULLIF(TRIM(dt.kind_name), '')
            )
        ),
        ''
    ) AS "Тип и наименование СЭМД",
    o.kind_code::text AS "Код СЭМД",
    COALESCE(dt.kind_name, o.kind_code::varchar) AS "Наименование СЭМД",
    o.document_id AS "localUid СЭМД",
    NULL::varchar(256) AS "Рег. номер РЭМД",
    'ожидание ответа'::text AS "Статус",
    NULL::text AS "Сводка ошибок",
    '[]'::jsonb AS "Ошибки JSON",
    NULL::timestamptz AS "Зарегистрирован в ЕГИСЗ РЭМД",
    NULL::timestamptz AS "Создание СЭМД",
    DATE(o.sent_at) AS "День (тренд)",
    COALESCE(NULLIF(TRIM(dc.jname), ''), 'Клиника JID: ' || COALESCE(o.jid::varchar, 'неизвестен')) AS "Наименование клиники",
    dc.jinn AS "ИНН клиники",
    dc.fir_oid AS "OID клиники",
    NULL::text AS "LOGID журнала EXCHANGELOG",
    o.egmid::text AS "EGISZ_MESSAGES.EGMID (ключ записи, РЭМД)",
    COALESCE(
        CASE
            WHEN o.gost_jid_token IS NOT NULL AND TRIM(o.gost_jid_token) <> ''
                THEN 'gost-' || o.gost_jid_token || '.infoclinica.lan'
        END,
        LEFT(o.reply_to, 512)
    ) AS "Хост клиники (VPN ГОСТ)",
    NULL::varchar(256) AS "OID организации",
    o.gost_jid_token AS "Токен gost (нецифр., для отображения)",
    NULL::text AS "JID (EGISZ_LICENSES)",
    NULL::text AS "JID из gost в LOGTEXT",
    NULL::text AS "JID из gost в REPLYTO",
    NULL::text AS "Токен gost (LOGTEXT)",
    NULL::text AS "Токен gost (REPLYTO)",
    false AS "Расхождение источников JID",
    NULL::text AS "MSGID обмена (EXCHANGELOG)",
    NULL::varchar(256) AS "Связанное сообщение"
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
  )
UNION ALL
SELECT
    j.msg_created_at AS "Дата обработки",
    NULL::text AS "JID",
    NULL::text AS "Тип и наименование СЭМД",
    NULL::text AS "Код СЭМД",
    NULL::varchar AS "Наименование СЭМД",
    TRIM(j.documentid) AS "localUid СЭМД",
    NULL::varchar(256) AS "Рег. номер РЭМД",
    'ожидание ответа'::text AS "Статус",
    NULL::text AS "Сводка ошибок",
    '[]'::jsonb AS "Ошибки JSON",
    NULL::timestamptz AS "Зарегистрирован в ЕГИСЗ РЭМД",
    NULL::timestamptz AS "Создание СЭМД",
    DATE(j.msg_created_at) AS "День (тренд)",
    'Клиника JID: неизвестен'::varchar(512) AS "Наименование клиники",
    NULL::varchar(12) AS "ИНН клиники",
    NULL::varchar(255) AS "OID клиники",
    NULL::text AS "LOGID журнала EXCHANGELOG",
    j.egmid::text AS "EGISZ_MESSAGES.EGMID (ключ записи, РЭМД)",
    LEFT(TRIM(j.replyto), 512) AS "Хост клиники (VPN ГОСТ)",
    NULL::varchar(256) AS "OID организации",
    NULL::text AS "Токен gost (нецифр., для отображения)",
    NULL::text AS "JID (EGISZ_LICENSES)",
    NULL::text AS "JID из gost в LOGTEXT",
    NULL::text AS "JID из gost в REPLYTO",
    NULL::text AS "Токен gost (LOGTEXT)",
    NULL::text AS "Токен gost (REPLYTO)",
    false AS "Расхождение источников JID",
    j.msgid::text AS "MSGID обмена (EXCHANGELOG)",
    NULL::varchar(256) AS "Связанное сообщение"
FROM stg_egisz_messages_journal j
WHERE j.documentid IS NOT NULL
  AND TRIM(j.documentid) <> ''
  AND NOT EXISTS (
    SELECT 1
    FROM fact_egisz_transactions f
    WHERE f.local_uid_semd IS NOT NULL
      AND TRIM(f.local_uid_semd) = TRIM(j.documentid)
  )
  AND NOT EXISTS (
    SELECT 1
    FROM stg_egisz_outbound_documents o
    WHERE o.document_id IS NOT NULL
      AND TRIM(o.document_id) = TRIM(j.documentid)
  );

COMMENT ON VIEW v_rpt_semd_archive_ui IS 'Все СЭМД по localUid: обработанные колбэки (fact) UNION ALL исходящие без факта из outbound-снимка UNION ALL строки только из журнала сообщений (ещё не в outbound). Статус «ожидание ответа» — нет строки в fact_egisz_transactions.';

CREATE OR REPLACE VIEW v_rpt_documents_no_response_ui AS
SELECT
    local_uid_semd AS "localUid СЭМД",
    kind_code::text AS "Код СЭМД",
    kind_name AS "Наименование СЭМД",
    jid::text AS "JID клиники",
    clinic_name AS "Наименование клиники",
    gost_host AS "Хост клиники (VPN ГОСТ)",
    sent_at AS "Отправлено"
FROM v_rpt_documents_no_response;

COMMENT ON VIEW v_rpt_documents_no_response_ui IS 'Обёртка над v_rpt_documents_no_response с подписями колонок для отчётов; см. dim_column_display_labels. JID клиники и Код СЭМД — TEXT (идентификаторы, не суммируются в Metabase).';
