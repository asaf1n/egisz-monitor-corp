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
    semd_creation_at TIMESTAMPTZ,
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

ALTER TABLE fact_egisz_transactions ADD COLUMN IF NOT EXISTS semd_creation_at TIMESTAMPTZ;

COMMENT ON COLUMN fact_egisz_transactions.registration_date IS 'Дата/время регистрации в ЕГИСЗ РЭМД: тег registrationDateTime (предпочтительно) или registrationDate в XML из EXCHANGELOG.MSGTEXT';
COMMENT ON COLUMN fact_egisz_transactions.semd_creation_at IS 'Дата/время создания СЭМД: тег creationDateTime в XML из EXCHANGELOG.MSGTEXT';
COMMENT ON COLUMN fact_egisz_transactions.processed_at IS 'Обработано IPS: EGISZ_MESSAGES.CREATEDATE; при отсутствии джойна — CREATEDATE строки EXCHANGELOG; иначе время загрузки ETL';

ALTER TABLE fact_egisz_transactions ADD COLUMN IF NOT EXISTS exchangelog_log_id BIGINT;
ALTER TABLE fact_egisz_transactions ADD COLUMN IF NOT EXISTS egisz_messages_egmid BIGINT;

COMMENT ON COLUMN fact_egisz_transactions.exchangelog_log_id IS 'EXCHANGELOG.LOGID строки журнала-источника (как водяной знак ETL, но на уровне факта для отчётов)';
COMMENT ON COLUMN fact_egisz_transactions.egisz_messages_egmid IS 'EGISZ_MESSAGES.EGMID: из LEFT JOIN по MSGID в выгрузке журнала или из кэша дозагрузки по MSGID';

CREATE INDEX IF NOT EXISTS idx_fact_egisz_exchangelog_log_id ON fact_egisz_transactions (exchangelog_log_id);
CREATE INDEX IF NOT EXISTS idx_fact_egisz_egisz_messages_egmid ON fact_egisz_transactions (egisz_messages_egmid);

CREATE TABLE IF NOT EXISTS stg_parse_errors (
    id BIGSERIAL PRIMARY KEY,
    relates_to_id VARCHAR(256),
    error_code VARCHAR(64) NOT NULL,
    message TEXT NOT NULL,
    log_excerpt TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

COMMENT ON TABLE stg_parse_errors IS 'Rows where MSGTEXT could not yield relates_to_id or XML is unusable';

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
    CASE
        WHEN f.gost_jid_token IS NOT NULL AND TRIM(f.gost_jid_token) <> ''
            THEN 'gost-' || f.gost_jid_token || '.infoclinica.lan'
    END AS gost_host,
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
    dc.fir_oid AS clinic_mo_oid
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

COMMENT ON TABLE stg_egisz_outbound_documents IS 'EGISZ_MESSAGES с непустым DOCUMENTID (EGMID выше курсора ETL); для отчёта «Документы без ответа»';
COMMENT ON COLUMN stg_egisz_outbound_documents.egmid IS 'EGMID строки EGISZ_MESSAGES при последнем снимке staging';
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
    ('v_egisz_transactions_enriched', 'exchangelog_log_id', 'LOGID журнала EXCHANGELOG'),
    ('v_egisz_transactions_enriched', 'egisz_messages_egmid', 'EGMID сообщения EGISZ_MESSAGES'),
    ('v_egisz_transactions_enriched', 'local_uid_semd', 'localUid СЭМД'),
    ('v_egisz_transactions_enriched', 'jid', 'JID клиники'),
    ('v_egisz_transactions_enriched', 'gost_jid_token', 'Фрагмент токена gost (LOGTEXT)'),
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
    egisz_messages_egmid::text AS "EGMID сообщения EGISZ_MESSAGES",
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
    relates_to_id AS "Связанное сообщение"
FROM v_egisz_transactions_enriched;

COMMENT ON VIEW v_egisz_transactions_enriched_ui IS 'Обёртка над v_egisz_transactions_enriched с подписями колонок для отчётов; см. dim_column_display_labels. JID клиники, Код СЭМД, LOGID/EGMID — TEXT (идентификаторы: без разделителей тысяч и суммирования в Metabase). «Сводка ошибок» — errors_friendly: агрегация подсказок по errors_json, исходные «Ошибки JSON» не меняются. Колонка «Связанное сообщение» (relates_to_id) — последняя для удобства витрин и Metabase.';

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
