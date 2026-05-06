-- EGISZ corporate DWH (Metabase-facing). Полная схема витрины в одном файле.
-- UPSERT key: relates_to_id (SOAP callback). Watermark Firebird: EXCHANGELOG.LOGID → etl_state.
-- MSGID — идентификатор сообщения в обмене; EXCHANGELOG.MSGID ссылается на EGISZ_MESSAGES.MSGID; EGMID — суррогатный ключ строки EGISZ_MESSAGES (РЭМД).
-- Источник полей клиники: Firebird JPERSONS / EGISZ_LICENSES (см. proxy_tables: JINN VARCHAR(12), FIR_OID VARCHAR(255)).

CREATE TABLE IF NOT EXISTS dim_semd_types (
    kind_code VARCHAR(16) PRIMARY KEY,
    kind_name VARCHAR(512) NOT NULL
);

COMMENT ON TABLE dim_semd_types IS 'Типы СЭМД: kind_code — идентификатор записи НСИ, kind_name — наименование (НСИ 1.2.643.5.1.13.13.11.1520; паспорт https://nsi.rosminzdrav.ru/dictionaries/1.2.643.5.1.13.13.11.1520/passport/12.33).';

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

-- Сопоставление stg_channel_errors ↔ fact в v_rpt_network_errors_detail: условие LATERAL использует
-- lower(trim(...)) по relates_to_id / local_uid_semd; без выраженных индексов — Seq Scan на факте на каждую строку сетевой ошибки.
CREATE INDEX IF NOT EXISTS idx_fact_relates_id_lower_trim ON fact_egisz_transactions ((lower(trim(relates_to_id))));
CREATE INDEX IF NOT EXISTS idx_fact_local_uid_lower_trim ON fact_egisz_transactions ((lower(trim(local_uid_semd))))
    WHERE local_uid_semd IS NOT NULL;

DO $$
BEGIN
  IF to_regclass('public.stg_parse_errors') IS NOT NULL
     AND to_regclass('public.stg_channel_errors') IS NULL THEN
    ALTER TABLE public.stg_parse_errors RENAME TO stg_channel_errors;
  END IF;
END $$;

DROP VIEW IF EXISTS v_stg_parse_errors_by_document CASCADE;

CREATE TABLE IF NOT EXISTS stg_channel_errors (
    id BIGSERIAL PRIMARY KEY,
    relates_to_id VARCHAR(256),
    error_code VARCHAR(64) NOT NULL,
    message TEXT NOT NULL,
    log_excerpt TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

ALTER TABLE stg_channel_errors ADD COLUMN IF NOT EXISTS exchangelog_log_id BIGINT;
ALTER TABLE stg_channel_errors ADD COLUMN IF NOT EXISTS egisz_messages_egmid BIGINT;
ALTER TABLE stg_channel_errors ADD COLUMN IF NOT EXISTS journal_msgid VARCHAR(256);
ALTER TABLE stg_channel_errors ADD COLUMN IF NOT EXISTS error_top_type VARCHAR(32);
ALTER TABLE stg_channel_errors ADD COLUMN IF NOT EXISTS error_group VARCHAR(32);
ALTER TABLE stg_channel_errors ADD COLUMN IF NOT EXISTS error_subtype VARCHAR(64);
ALTER TABLE stg_channel_errors ADD COLUMN IF NOT EXISTS relates_to_hint VARCHAR(512);
ALTER TABLE stg_channel_errors ADD COLUMN IF NOT EXISTS local_uid_hint VARCHAR(512);
ALTER TABLE stg_channel_errors ADD COLUMN IF NOT EXISTS emdr_id_hint VARCHAR(512);
ALTER TABLE stg_channel_errors ADD COLUMN IF NOT EXISTS proxy_context_at TIMESTAMPTZ;

COMMENT ON TABLE stg_channel_errors IS 'События канала интеграции без построенного факта ответа РЭМД: ошибка связи (EXCHANGELOG LOGSTATE=3 и др.) или ошибка в асинхронном ответе. Источник — прокси-база сервиса интеграции. Классификация: error_top_type, error_group, error_subtype.';
COMMENT ON COLUMN stg_channel_errors.proxy_context_at IS 'Время в контексте прокси-журнала: CREATEDATE сообщения EGISZ_MESSAGES (если есть), иначе CREATEDATE строки EXCHANGELOG; для отчётов как «дата создания документа» вместе с моментом загрузки в аналитику (created_at).';
COMMENT ON COLUMN stg_channel_errors.exchangelog_log_id IS 'EXCHANGELOG.LOGID — исходная строка прокси-журнала (трассировка)';
COMMENT ON COLUMN stg_channel_errors.egisz_messages_egmid IS 'EGISZ_MESSAGES.EGMID — идентификатор записи сообщения (трассировка)';
COMMENT ON COLUMN stg_channel_errors.journal_msgid IS 'EXCHANGELOG.MSGID — идентификатор сообщения в контуре обмена (трассировка к EGISZ_MESSAGES.MSGID)';
COMMENT ON COLUMN stg_channel_errors.error_top_type IS 'Глобальный тип: network = ошибка связи/канала; async_response = ошибка в асинхронном ответе РЭМД (разбор/связка)';
COMMENT ON COLUMN stg_channel_errors.error_group IS 'Внутренняя группа: network | parse | linkage | identifiers | other';
COMMENT ON COLUMN stg_channel_errors.error_subtype IS 'Подтип внутри группировки (стабильный идентификатор для отчётов)';
COMMENT ON COLUMN stg_channel_errors.relates_to_hint IS 'Регексп по тегу relatesToMessage в сыром MSGTEXT, если факт не построен';
COMMENT ON COLUMN stg_channel_errors.local_uid_hint IS 'Регексп по localUid в сыром MSGTEXT (экземпляр СЭМД в МИС)';
COMMENT ON COLUMN stg_channel_errors.emdr_id_hint IS 'Регексп по emdrId в сыром MSGTEXT (рег. номер в РЭМД, если уже фигурирует в теле)';

-- Дашборды по сетевым ошибкам: фильтр по бизнес-времени прокси-журнала и по моменту загрузки.
CREATE INDEX IF NOT EXISTS idx_stg_channel_errors_network_created_at ON stg_channel_errors (created_at DESC, id)
WHERE error_top_type = 'network'
   OR UPPER(COALESCE(error_code::text, '')) IN ('NETWORK_ERROR', 'INTEGRATION_LOGSTATE_3');
CREATE INDEX IF NOT EXISTS idx_stg_channel_errors_network_proxy_ctx ON stg_channel_errors (proxy_context_at DESC NULLS LAST, id)
WHERE error_top_type = 'network'
   OR UPPER(COALESCE(error_code::text, '')) IN ('NETWORK_ERROR', 'INTEGRATION_LOGSTATE_3');

-- Ключ для агрегации «один документ — одна ошибка» в Metabase/healthcheck: coalesce идентификаторов, иначе уникальный id строки.
CREATE OR REPLACE VIEW v_stg_channel_errors_by_document AS
SELECT
    s.*,
    COALESCE(
        NULLIF(TRIM(s.relates_to_hint), ''),
        NULLIF(TRIM(s.local_uid_hint), ''),
        NULLIF(TRIM(s.emdr_id_hint), ''),
        NULLIF(TRIM(s.relates_to_id), ''),
        'id:' || s.id::text
    ) AS document_group_key,
    CASE
        WHEN s.error_top_type = 'network'
            OR UPPER(COALESCE(s.error_code, '')) IN ('NETWORK_ERROR', 'INTEGRATION_LOGSTATE_3') THEN 'Ошибка связи'
        WHEN s.error_group = 'parse' THEN 'Ошибка парсинга'
        ELSE 'Ошибка в асинхронном ответе РЭМД'
    END AS error_global_subcategory,
    CASE s.error_group
        WHEN 'network' THEN 'Канал: связь и транспорт'
        WHEN 'parse' THEN 'Колбэк РЭМД: разбор XML/SOAP'
        WHEN 'linkage' THEN 'Колбэк РЭМД: связка запрос–ответ'
        WHEN 'identifiers' THEN 'Колбэк РЭМД: идентификаторы документа'
        WHEN 'other' THEN 'Колбэк РЭМД: прочее'
        ELSE COALESCE(s.error_group, '')
    END AS error_group_label_ru,
    CASE s.error_subtype
        WHEN 'logstate_3' THEN 'Журнал: LOGSTATE=3 (связь/транспорт; детали в message, URL/host нормализованы)'
        WHEN 'xml_broken' THEN 'Неразборный XML колбэка'
        WHEN 'msgtext_too_large' THEN 'MSGTEXT превышает лимит ETL'
        WHEN 'missing_relates_to_message' THEN 'Нет relatesToMessage'
        WHEN 'missing_localuid_documentid_emdrid' THEN 'Нет localUid / DOCUMENTID / emdrId'
        ELSE COALESCE(s.error_subtype, '')
    END AS error_subtype_label_ru
FROM stg_channel_errors s;

COMMENT ON VIEW v_stg_channel_errors_by_document IS 'Надстройка над stg_channel_errors: document_group_key и русские подписи классификации.';

-- Агрегированная «интерпретация ошибок» по errors_json: одна строка на факт, без искажения сырого ответа.
-- Разбор нескольких блоков Schematron в одном message: разделитель внутри элемента — " — ".

CREATE OR REPLACE FUNCTION egisz_error_interpretation_schematron_chunk(p_chunk text)
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

COMMENT ON FUNCTION egisz_error_interpretation_schematron_chunk IS 'Интерпретация одного блока Schematron: сжатие длинного текста до короткой подсказки, без изменения исходного errors_json.';

CREATE OR REPLACE FUNCTION egisz_friendly_schematron_chunk(p_chunk text)
RETURNS text
LANGUAGE sql
IMMUTABLE
AS $wrap$
  SELECT egisz_error_interpretation_schematron_chunk(p_chunk);
$wrap$;

COMMENT ON FUNCTION egisz_friendly_schematron_chunk IS 'DEPRECATED: используйте egisz_error_interpretation_schematron_chunk.';

CREATE OR REPLACE FUNCTION egisz_error_interpretation_item(p_code text, p_message text)
RETURNS text
LANGUAGE plpgsql
STABLE
AS $e$
DECLARE
  m text;
  c text;
  parts text[];
  chunk text;
  out_parts text[] := ARRAY[]::text[];
  deduped text[] := ARRAY[]::text[];
  p text;
  n int;
  i int;
BEGIN
  c := upper(trim(COALESCE(p_code, '')));
  m := trim(COALESCE(p_message, ''));
  IF m = '' THEN
    IF nullif(c, '') IS NOT NULL THEN
      RETURN 'Код: ' || c;
    END IF;
    RETURN NULL;
  END IF;

  -- Интерпретация кодов сообщений РЭМД: справочник НСИ Минздрава
  -- 1.2.643.5.1.13.13.99.2.305 — «РЭМД. Классификатор кодов сообщений» (см. nsi.rosminzdrav.ru).
  IF c IN ('RUNTIME_ERROR', 'INTERNAL_ERROR') THEN
    RETURN 'Техническая ошибка на стороне РЭМД (федеральная): повторите отправку позже';
  END IF;
  IF c IN ('CA_INACCESSIBILITY', 'CA_UNAVAILABLE') THEN
    RETURN 'Недоступен сервис проверки подписи/УЦ на стороне РЭМД: повторите отправку позже';
  END IF;
  IF c IN ('ASYNC_RESPONSE_TIMEOUT', 'TIMEOUT') THEN
    RETURN 'Таймаут асинхронной обработки на стороне РЭМД: повторите отправку позже';
  END IF;

  -- Ошибки XSD/cvc валидации: короткая подсказка без раздувания.
  IF m ~* '\bcvc-' OR m ~* 'XML_VALIDATION_ERROR' OR m ~* 'xsd' THEN
    RETURN 'Ошибка XSD-валидации XML (cvc-): проверьте обязательные поля/формат в СЭМД';
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
    out_parts := array_append(out_parts, egisz_error_interpretation_schematron_chunk(chunk));
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

COMMENT ON FUNCTION egisz_error_interpretation_item IS 'Одна строка интерпретации по code+message; Schematron с несколькими блоками склеивает " — "; исходный текст, если нет schematron/схематрона.';

CREATE OR REPLACE FUNCTION egisz_friendly_error_item(p_code text, p_message text)
RETURNS text
LANGUAGE sql
STABLE
AS $wrap$
  SELECT egisz_error_interpretation_item(p_code, p_message);
$wrap$;

COMMENT ON FUNCTION egisz_friendly_error_item IS 'DEPRECATED: используйте egisz_error_interpretation_item.';

-- Нормализованный "тип ошибки" для группировок в Metabase:
-- объединяет сообщения, которые отличаются только переменными (ФИО/адрес/наименование/ID/URL/даты/числа).
CREATE OR REPLACE FUNCTION egisz_error_interpretation_type(p_code text, p_message text)
RETURNS text
LANGUAGE plpgsql
STABLE
AS $t$
DECLARE
  s text;
BEGIN
  s := trim(COALESCE(egisz_error_interpretation_item(p_code, p_message), ''));
  IF s = '' THEN
    RETURN NULL;
  END IF;

  -- URL/host: чтобы разные endpoints не дробили тип ошибки.
  s := regexp_replace(s, '\bhttps?://[^\s<>"\)]+', '<url>', 'gi');
  s := regexp_replace(
    s,
    '\b(?:(?:gost-[a-z0-9_-]+\.infoclinica\.lan)|(?:\d{1,3}(?:\.\d{1,3}){3})(?::\d{1,5})?)\b',
    '<host>',
    'gi'
  );

  -- UUID / хэши / длинные числа.
  s := regexp_replace(s, '\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b', '<uuid>', 'gi');
  s := regexp_replace(s, '\b[0-9a-f]{16,}\b', '<hex>', 'gi');
  s := regexp_replace(s, '\b\d{5,}\b', '<n>', 'g');

  -- Даты/время в ISO-стиле.
  s := regexp_replace(
    s,
    '\b\d{4}-\d{2}-\d{2}(?:[ T]\d{2}:\d{2}:\d{2}(?:\.\d+)?)?(?:Z|[+-]\d{2}:\d{2})?\b',
    '<dt>',
    'g'
  );

  -- Частый кейс РЭМД: "Указанное значение (Имя пациента) <ФИО> не соответствует ..."
  s := regexp_replace(
    s,
    '(Указанное значение\s*\([^)]*\)\s*)(["«]?)\s*[^"»\r\n]{2,160}\s*\2(\s*не соотв[а-яё]*\b)',
    '\1<значение>\3',
    'gi'
  );

  -- ФИО в контексте пациента/персоны (не глобально по всему тексту).
  s := regexp_replace(
    s,
    '(\b(?:имя пациента|фио пациента|пациент)\b[^:]{0,40}[:\s(]*)([А-ЯЁ][а-яё]+(?:\s+[А-ЯЁ][а-яё]+){1,3})',
    '\1<ФИО>',
    'gi'
  );

  -- Адрес (грубая нормализация по ключевому слову).
  s := regexp_replace(
    s,
    '(\bадрес\b[^:]{0,40}[:\s(]*)([^;,\r\n]{4,160})',
    '\1<адрес>',
    'gi'
  );

  -- Наименование (организации/клиники/прочее) в частых формулировках.
  s := regexp_replace(
    s,
    '(\bнаименовани[е-я]+\b[^:]{0,40}[:\s(]*)([^;,\r\n]{2,160})',
    '\1<наименование>',
    'gi'
  );

  -- Финальная нормализация пробелов.
  s := regexp_replace(s, '\s+', ' ', 'g');
  RETURN trim(s);
END;
$t$;

COMMENT ON FUNCTION egisz_error_interpretation_type IS 'Нормализованный тип ошибки для группировок: egisz_error_interpretation_item + замена переменных (ФИО/адрес/ID/URL/даты/числа) на плейсхолдеры.';

CREATE OR REPLACE FUNCTION egisz_friendly_error_type(p_code text, p_message text)
RETURNS text
LANGUAGE sql
STABLE
AS $wrap$
  SELECT egisz_error_interpretation_type(p_code, p_message);
$wrap$;

COMMENT ON FUNCTION egisz_friendly_error_type IS 'DEPRECATED: используйте egisz_error_interpretation_type.';

CREATE OR REPLACE FUNCTION egisz_error_interpretation_row(p_errors jsonb)
RETURNS text
LANGUAGE sql
STABLE
AS $r$
  WITH items AS (
    SELECT
      o,
      NULLIF(trim(egisz_error_interpretation_item(e->>'code', e->>'message')), '') AS t
    FROM jsonb_array_elements(COALESCE(p_errors, '[]'::jsonb)) WITH ORDINALITY AS x(e, o)
  ),
  first_pos AS (
    SELECT t, MIN(o) AS first_o
    FROM items
    WHERE t IS NOT NULL
    GROUP BY t
  )
  SELECT NULLIF(string_agg(t, ' · ' ORDER BY first_o), '')
  FROM first_pos;
$r$;

COMMENT ON FUNCTION egisz_error_interpretation_row IS 'Интерпретация по массиву errors_json: элементы разделены " · " (средняя точка).';

CREATE OR REPLACE FUNCTION egisz_friendly_errors_row(p_errors jsonb)
RETURNS text
LANGUAGE sql
STABLE
AS $r$
  SELECT egisz_error_interpretation_row(p_errors);
$r$;

COMMENT ON FUNCTION egisz_friendly_errors_row IS 'DEPRECATED: используйте egisz_error_interpretation_row.';

-- Подпись типа СЭМД для отчётов: код НСИ обязателен для различения редакций (119 ≠ 227).
CREATE OR REPLACE FUNCTION egisz_semd_type_report_label(p_kind_code text, p_kind_name text)
RETURNS text
LANGUAGE sql
IMMUTABLE
AS $stl$
  SELECT LEFT(
      CASE
          WHEN NULLIF(TRIM(COALESCE(p_kind_code, '')), '') IS NOT NULL
               AND NULLIF(TRIM(COALESCE(p_kind_name, '')), '') IS NOT NULL
              THEN TRIM(p_kind_code) || ' · ' || TRIM(p_kind_name)
          WHEN NULLIF(TRIM(COALESCE(p_kind_code, '')), '') IS NOT NULL
              THEN TRIM(p_kind_code)
          WHEN NULLIF(TRIM(COALESCE(p_kind_name, '')), '') IS NOT NULL
              THEN TRIM(p_kind_name)
          ELSE '(неизвестно)'
      END,
      220
  );
$stl$;

COMMENT ON FUNCTION egisz_semd_type_report_label IS 'Тип СЭМД для отчётов Metabase: «код · наименование НСИ» по справочнику регистрируемых ЭМД (OID 1.2.643.5.1.13.13.11.1520, паспорт https://nsi.rosminzdrav.ru/dictionaries/1.2.643.5.1.13.13.11.1520/passport/12.33); каждый код — отдельный документ ЕГИСЗ.';

-- Совместимость: прежнее имя функции (устаревшая семантика «слияния по имени» снята).
CREATE OR REPLACE FUNCTION egisz_semd_report_group_label(p_kind_code text, p_kind_name text)
RETURNS text
LANGUAGE sql
IMMUTABLE
AS $legacy$
  SELECT egisz_semd_type_report_label(p_kind_code, p_kind_name);
$legacy$;

COMMENT ON FUNCTION egisz_semd_report_group_label IS 'DEPRECATED: используйте egisz_semd_type_report_label (код · наименование НСИ).';

-- Единый ключ документа для учёта дубликатов по идентификаторам (регистр UUID несущественен).
CREATE OR REPLACE FUNCTION egisz_document_identity_key(
    p_relates_to text,
    p_local_uid text,
    p_emdr_id text
)
RETURNS text
LANGUAGE sql
IMMUTABLE
AS $dk$
  SELECT COALESCE(
      NULLIF(LOWER(TRIM(COALESCE(p_relates_to, ''))), ''),
      NULLIF(LOWER(TRIM(COALESCE(p_local_uid, ''))), ''),
      NULLIF(LOWER(TRIM(COALESCE(p_emdr_id, ''))), '')
  );
$dk$;

COMMENT ON FUNCTION egisz_document_identity_key IS 'Один документ в контуре: приоритет relatesToMessage (колбэк), иначе localUid/DOCUMENTID, иначе emdrId; для сравнения без расхождений регистра.';

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
DROP VIEW IF EXISTS v_rpt_connectivity_global_daily_ui;
DROP VIEW IF EXISTS v_rpt_clinic_connectivity_daily_ui;
DROP VIEW IF EXISTS v_rpt_clinic_connectivity_daily;
DROP VIEW IF EXISTS v_rpt_network_errors_detail_ui;
DROP VIEW IF EXISTS v_rpt_network_errors_detail;
DROP VIEW IF EXISTS v_stg_channel_network_errors_by_document;
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
    CASE
        WHEN f.status = 'error' THEN 'ошибка асинхронного ответа РЭМД'
        ELSE NULL
    END AS error_global_subcategory,
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
    egisz_semd_type_report_label(NULLIF(TRIM(f.kind_code::text), ''), NULLIF(TRIM(COALESCE(dt.kind_name::text, '')), '')) AS semd_type_label,
    egisz_document_identity_key(f.relates_to_id, f.local_uid_semd, f.emdr_id) AS document_identity_key,
    CASE WHEN f.relates_to_id IS NOT NULL THEN LOWER(TRIM(f.relates_to_id)) END AS relates_to_canonical,
    CASE WHEN f.local_uid_semd IS NOT NULL THEN LOWER(TRIM(f.local_uid_semd)) END AS local_uid_canonical,
    f.status,
    f.emdr_id,
    f.errors_json,
    egisz_error_interpretation_row(f.errors_json) AS errors_interpretation,
    egisz_error_interpretation_row(f.errors_json) AS errors_friendly,
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
      AND LOWER(TRIM(f.local_uid_semd)) = LOWER(TRIM(o.document_id))
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
      AND LOWER(TRIM(f.local_uid_semd)) = LOWER(TRIM(j.documentid))
  )
  AND NOT EXISTS (
    SELECT 1
    FROM stg_egisz_outbound_documents o
    WHERE o.document_id IS NOT NULL
      AND LOWER(TRIM(o.document_id)) = LOWER(TRIM(j.documentid))
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

DELETE FROM dim_column_display_labels WHERE source_object = 'v_stg_parse_errors_by_document';

INSERT INTO dim_column_display_labels (source_object, source_column, display_label_ru) VALUES
    ('v_egisz_transactions_enriched', 'relates_to_id', 'Связанное сообщение'),
    ('v_egisz_transactions_enriched', 'exchangelog_log_id', 'LOGID журнала EXCHANGELOG'),
    ('v_egisz_transactions_enriched', 'egisz_messages_egmid', 'EGISZ_MESSAGES.EGMID (ключ записи сообщения, РЭМД)'),
    ('v_egisz_transactions_enriched', 'local_uid_semd', 'localUid СЭМД'),
    ('v_egisz_transactions_enriched', 'jid', 'JID клиники'),
    ('v_egisz_transactions_enriched', 'gost_jid_token', 'Токен gost (нецифр., для отображения)'),
    ('v_egisz_transactions_enriched', 'error_global_subcategory', 'Подкатегория ошибки (глобально)'),
    ('v_egisz_transactions_enriched', 'gost_host', 'Хост клиники (VPN ГОСТ)'),
    ('v_egisz_transactions_enriched', 'org_oid', 'OID организации'),
    ('v_egisz_transactions_enriched', 'kind_code', 'Код СЭМД'),
    ('v_egisz_transactions_enriched', 'kind_name', 'Наименование СЭМД'),
    ('v_egisz_transactions_enriched', 'semd_type_label', 'Тип СЭМД (код · наименование НСИ)'),
    ('v_egisz_transactions_enriched', 'document_identity_key', 'Документ (ключ учёта: relatesTo / localUid / emdr)'),
    ('v_egisz_transactions_enriched', 'relates_to_canonical', 'Связанное сообщение (канон для сравнения)'),
    ('v_egisz_transactions_enriched', 'local_uid_canonical', 'localUid СЭМД (канон для сравнения)'),
    ('v_egisz_transactions_enriched', 'status', 'Статус'),
    ('v_egisz_transactions_enriched', 'emdr_id', 'Рег. номер РЭМД (emdrid)'),
    ('v_egisz_transactions_enriched', 'errors_json', 'Ошибки JSON'),
    (
        'v_egisz_transactions_enriched',
        'errors_interpretation',
        'Интерпретация ошибок: одна строка; внутри одного сообщения Schematron блоки — «—», несколько item в JSON — «·»'
    ),
    (
        'v_egisz_transactions_enriched',
        'errors_friendly',
        'Интерпретация ошибок (устар.): используйте errors_interpretation'
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
    ('v_rpt_documents_no_response', 'sent_at', 'Отправлено'),
    (
        'v_stg_channel_errors_by_document',
        'error_global_subcategory',
        'Глобальный класс: Ошибка связи | Ошибка в асинхронном ответе РЭМД'
    ),
    ('v_stg_channel_errors_by_document', 'error_group_label_ru', 'Внутренняя группа (рус.)'),
    (
        'v_stg_channel_errors_by_document',
        'error_subtype_label_ru',
        'Подтип (рус.); переменные адреса/URL — в message (нормализация для агрегатов)'
    ),
    (
        'v_stg_channel_network_errors_by_document',
        'error_global_subcategory',
        'Глобальный класс: Ошибка связи | Ошибка в асинхронном ответе РЭМД'
    ),
    ('v_stg_channel_network_errors_by_document', 'error_group_label_ru', 'Внутренняя группа (рус.)'),
    (
        'v_stg_channel_network_errors_by_document',
        'error_subtype_label_ru',
        'Подтип (рус.); переменные адреса/URL — в message (нормализация для агрегатов)'
    ),
    ('v_rpt_clinic_connectivity_daily_ui', 'День', 'Календарный день (UTC)'),
    ('v_rpt_clinic_connectivity_daily_ui', 'JID клиники (ключ)', 'JID для стыковки факта и сети'),
    ('v_rpt_clinic_connectivity_daily_ui', 'Доступность транспорта (прибл.), %', 'Прокси: успехи / (успехи + ошибки связи)'),
    ('v_rpt_connectivity_global_daily_ui', 'День', 'Календарный день (UTC)'),
    ('v_rpt_connectivity_global_daily_ui', 'Доступность транспорта (прибл.), %', 'Прокси доступности транспорта')
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
    semd_type_label AS "Тип СЭМД (код · НСИ)",
    document_identity_key AS "Документ (ключ учёта)",
    relates_to_canonical AS "Связанное сообщение (канон)",
    local_uid_canonical AS "localUid СЭМД (канон)",
    status AS "Статус",
    error_global_subcategory AS "Подкатегория ошибки (глобально)",
    emdr_id AS "Рег. номер РЭМД (emdrid)",
    errors_json AS "Ошибки JSON",
    errors_interpretation AS "Интерпретация ошибок",
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

COMMENT ON VIEW v_egisz_transactions_enriched_ui IS 'Обёртка над v_egisz_transactions_enriched с подписями колонок для отчётов; см. dim_column_display_labels. «Тип СЭМД (код · НСИ)» — egisz_semd_type_report_label (каждый код НСИ отдельно). «Документ (ключ учёта)» — egisz_document_identity_key (relatesToMessage приоритетнее localUid/emdr). Канонические колонки для сравнения без учёта регистра UUID: «Связанное сообщение (канон)», «localUid СЭМД (канон)». JID клиники, Код СЭМД, LOGID/EGMID — TEXT (идентификаторы: без разделителей тысяч и суммирования в Metabase). «Интерпретация ошибок» — errors_interpretation: агрегация интерпретаций по errors_json, исходные «Ошибки JSON» не меняются. «Сводка ошибок» оставлена как устаревший алиас. Колонка «Связанное сообщение» (relates_to_id) — последняя для удобства витрин и Metabase.';

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
    e.semd_type_label AS "Тип СЭМД (код · НСИ)",
    e.document_identity_key AS "Документ (ключ учёта)",
    e.local_uid_semd AS "localUid СЭМД",
    e.emdr_id AS "Рег. номер РЭМД",
    e.status::text AS "Статус",
    e.errors_interpretation AS "Интерпретация ошибок",
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
    egisz_semd_type_report_label(NULLIF(TRIM(o.kind_code::text), ''), NULLIF(TRIM(COALESCE(dt.kind_name::text, '')), '')) AS "Тип СЭМД (код · НСИ)",
    egisz_document_identity_key(NULL::varchar, o.document_id, NULL::varchar) AS "Документ (ключ учёта)",
    o.document_id AS "localUid СЭМД",
    NULL::varchar(256) AS "Рег. номер РЭМД",
    'ожидание ответа'::text AS "Статус",
    NULL::text AS "Интерпретация ошибок",
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
      AND LOWER(TRIM(f.local_uid_semd)) = LOWER(TRIM(o.document_id))
  )
UNION ALL
SELECT
    j.msg_created_at AS "Дата обработки",
    NULL::text AS "JID",
    NULL::text AS "Тип и наименование СЭМД",
    NULL::text AS "Код СЭМД",
    NULL::varchar AS "Наименование СЭМД",
    '(неизвестно)'::text AS "Тип СЭМД (код · НСИ)",
    egisz_document_identity_key(NULL::varchar, TRIM(j.documentid), NULL::varchar) AS "Документ (ключ учёта)",
    TRIM(j.documentid) AS "localUid СЭМД",
    NULL::varchar(256) AS "Рег. номер РЭМД",
    'ожидание ответа'::text AS "Статус",
    NULL::text AS "Интерпретация ошибок",
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
      AND LOWER(TRIM(f.local_uid_semd)) = LOWER(TRIM(j.documentid))
  )
  AND NOT EXISTS (
    SELECT 1
    FROM stg_egisz_outbound_documents o
    WHERE o.document_id IS NOT NULL
      AND LOWER(TRIM(o.document_id)) = LOWER(TRIM(j.documentid))
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

-- Сетевые ошибки: только строки с идентификатором документа в staging (см. ETL LOGSTATE=3).
CREATE OR REPLACE VIEW v_stg_channel_network_errors_by_document AS
SELECT *
FROM v_stg_channel_errors_by_document s
WHERE (
    s.error_top_type = 'network'
    OR UPPER(COALESCE(s.error_code::text, '')) IN ('NETWORK_ERROR', 'INTEGRATION_LOGSTATE_3')
)
AND (
    NULLIF(TRIM(s.relates_to_id::text), '') IS NOT NULL
    OR NULLIF(TRIM(s.relates_to_hint::text), '') IS NOT NULL
    OR NULLIF(TRIM(s.local_uid_hint::text), '') IS NOT NULL
    OR NULLIF(TRIM(s.emdr_id_hint::text), '') IS NOT NULL
);

COMMENT ON VIEW v_stg_channel_network_errors_by_document IS 'Подмножество v_stg_channel_errors_by_document: ошибки связи с привязкой к документу; дашборды сети и v_rpt_network_errors_detail.';

-- Сетевые ошибки (LOGSTATE=3 / network): связка с колбэком РЭМД по relatesToMessage из сырого MSGTEXT и/или localUid.
CREATE OR REPLACE VIEW v_rpt_network_errors_detail AS
SELECT
    s.id AS staging_error_id,
    s.created_at,
    s.proxy_context_at,
    COALESCE(s.proxy_context_at, s.created_at) AS document_context_at,
    s.exchangelog_log_id,
    s.egisz_messages_egmid,
    s.journal_msgid,
    s.error_code,
    s.message AS network_error_message,
    s.relates_to_hint,
    s.local_uid_hint,
    s.emdr_id_hint,
    s.error_global_subcategory,
    s.error_group_label_ru,
    s.error_subtype_label_ru,
    s.document_group_key,
    lf.relates_to_id AS linked_relates_to_message,
    lf.local_uid_semd AS linked_local_uid_semd,
    lf.jid AS linked_jid,
    lf.kind_code::text AS linked_kind_code,
    ldt.kind_name AS linked_kind_name,
    egisz_semd_type_report_label(NULLIF(TRIM(lf.kind_code::text), ''), NULLIF(TRIM(COALESCE(ldt.kind_name::text, '')), '')) AS linked_semd_type_label,
    COALESCE(NULLIF(TRIM(ldc.jname), ''), CASE WHEN lf.jid IS NOT NULL THEN 'Клиника JID: ' || lf.jid::text ELSE NULL END) AS linked_clinic_name,
    lf.status::text AS linked_callback_status,
    egisz_error_interpretation_row(lf.errors_json) AS linked_errors_interpretation,
    lf.exchangelog_log_id AS linked_exchangelog_log_id,
    lf.egisz_messages_egmid AS linked_egisz_messages_egmid,
    lf.emdr_id AS linked_emdr_id,
    jgid.journal_gost_numeric_jid,
    COALESCE(
        COALESCE(NULLIF(TRIM(ldc.jname), ''), CASE WHEN lf.jid IS NOT NULL THEN 'Клиника JID: ' || lf.jid::text END),
        NULLIF(TRIM(ldj.jname), ''),
        CASE WHEN jgid.journal_gost_numeric_jid IS NOT NULL THEN 'Только JID из журнала: ' || jgid.journal_gost_numeric_jid::text END,
        '(клиника по gost/журналу не определена)'
    ) AS connectivity_clinic_label,
    (lf.relates_to_id IS NOT NULL) AS has_linked_callback_fact
FROM v_stg_channel_network_errors_by_document s
LEFT JOIN LATERAL (
    SELECT (regexp_match(COALESCE(s.log_excerpt, ''), 'gost-([0-9]+)\.infoclinica\.lan', 'i'))[1]::bigint AS journal_gost_numeric_jid
) jgid ON TRUE
LEFT JOIN dim_clinics ldj ON ldj.jid = jgid.journal_gost_numeric_jid
LEFT JOIN LATERAL (
    SELECT f.*
    FROM fact_egisz_transactions f
    WHERE (
        NULLIF(TRIM(s.relates_to_hint), '') IS NOT NULL
        AND LOWER(TRIM(s.relates_to_hint)) = LOWER(TRIM(f.relates_to_id))
    )
    OR (
        NULLIF(TRIM(s.local_uid_hint), '') IS NOT NULL
        AND LOWER(TRIM(s.local_uid_hint)) = LOWER(TRIM(f.local_uid_semd))
    )
    ORDER BY
        CASE
            WHEN NULLIF(TRIM(s.relates_to_hint), '') IS NOT NULL
                 AND LOWER(TRIM(s.relates_to_hint)) = LOWER(TRIM(f.relates_to_id))
            THEN 0
            ELSE 1
        END,
        f.processed_at DESC NULLS LAST
    LIMIT 1
) lf ON TRUE
LEFT JOIN dim_semd_types ldt ON ldt.kind_code = lf.kind_code
LEFT JOIN dim_clinics ldc ON ldc.jid = lf.jid;

COMMENT ON VIEW v_rpt_network_errors_detail IS 'Ошибки связи (транспорт, LOGSTATE=3): строки прокси-журнала с идентификатором документа (v_stg_channel_network_errors_by_document). Не смешивать с отказом регистрации в теле ответа РЭМД. Клиника: связанный колбэк в fact (если есть), иначе JID из gost в log_excerpt.';

CREATE OR REPLACE VIEW v_rpt_network_errors_detail_ui AS
SELECT
    document_context_at AS "Дата создания документа",
    exchangelog_log_id::text AS "LOGID журнала (сетевая ошибка)",
    egisz_messages_egmid::text AS "EGMID сообщения (строка журнала)",
    journal_msgid AS "MSGID обмена",
    error_code AS "Код ошибки канала",
    network_error_message AS "Текст сетевой ошибки",
    relates_to_hint AS "relatesToMessage (из текста журнала)",
    local_uid_hint AS "localUid / DOCUMENTID (из текста)",
    emdr_id_hint AS "emdrId (из текста)",
    error_subtype_label_ru AS "Подтип ошибки канала",
    document_group_key AS "Ключ документа (группировка)",
    linked_relates_to_message AS "Связанное сообщение (ответ РЭМД)",
    linked_local_uid_semd AS "Идентификатор документа (localUid)",
    linked_jid::text AS "JID клиники",
    linked_kind_code AS "Код СЭМД",
    linked_kind_name AS "Наименование СЭМД",
    linked_semd_type_label AS "Тип СЭМД (код · НСИ)",
    linked_clinic_name AS "Медицинская организация",
    linked_callback_status AS "Статус регистрации в РЭМД",
    linked_errors_interpretation AS "Интерпретация ошибок регистрации",
    linked_exchangelog_log_id::text AS "LOGID записи ответа",
    linked_egisz_messages_egmid::text AS "EGMID записи ответа",
    linked_emdr_id AS "Регистрационный номер РЭМД",
    journal_gost_numeric_jid::text AS "JID из журнала (gost, число)",
    connectivity_clinic_label AS "Клиника (транспорт)",
    has_linked_callback_fact AS "Связанный колбэк найден в аналитике"
FROM v_rpt_network_errors_detail;

COMMENT ON VIEW v_rpt_network_errors_detail_ui IS 'Сетевые ошибки (транспорт) по данным прокси-базы: «Клиника (транспорт)» — недоступность/доставка (связанный колбэк в fact и/или JID из gost в log_excerpt). «Дата создания документа» — COALESCE(proxy_context_at, created_at): время из EGISZ_MESSAGES/EXCHANGELOG при загрузке, иначе момент фиксации в аналитике.';

-- По дням и JID: ответы РЭМД (факт) vs ошибки связи (staging). Ключ клиники — COALESCE(linked_jid, gost-JID из журнала) для сети и jid факта для колбэков.
CREATE OR REPLACE VIEW v_rpt_clinic_connectivity_daily AS
SELECT
    COALESCE(f.day_bucket, n.day_bucket) AS day_bucket,
    COALESCE(f.clinic_jid_key, n.clinic_jid_key) AS clinic_jid_key,
    COALESCE(f.facts_success, 0)::bigint AS facts_success,
    COALESCE(f.facts_error_remd, 0)::bigint AS facts_error_remd,
    COALESCE(f.facts_total, 0)::bigint AS facts_total,
    COALESCE(n.network_document_errors, 0)::bigint AS network_document_errors,
    CASE
        WHEN COALESCE(f.facts_success, 0) + COALESCE(n.network_document_errors, 0) > 0 THEN
            ROUND(
                100.0 * COALESCE(f.facts_success, 0)
                / (COALESCE(f.facts_success, 0) + COALESCE(n.network_document_errors, 0)),
                2
            )
        ELSE NULL
    END AS availability_transport_proxy_pct
FROM (
    SELECT
        (date_trunc('day', f.processed_at AT TIME ZONE 'UTC'))::date AS day_bucket,
        COALESCE(f.jid::text, '(нет JID)') AS clinic_jid_key,
        COUNT(DISTINCT f.relates_to_id) FILTER (WHERE f.status = 'success') AS facts_success,
        COUNT(DISTINCT f.relates_to_id) FILTER (WHERE f.status = 'error') AS facts_error_remd,
        COUNT(DISTINCT f.relates_to_id) AS facts_total
    FROM fact_egisz_transactions f
    GROUP BY 1, 2
) f
FULL OUTER JOIN (
    SELECT
        (date_trunc('day', d.document_context_at AT TIME ZONE 'UTC'))::date AS day_bucket,
        COALESCE(COALESCE(d.linked_jid, d.journal_gost_numeric_jid)::text, '(нет JID)') AS clinic_jid_key,
        COUNT(DISTINCT d.document_group_key) AS network_document_errors
    FROM v_rpt_network_errors_detail d
    GROUP BY 1, 2
) n ON f.day_bucket = n.day_bucket AND f.clinic_jid_key = n.clinic_jid_key;

COMMENT ON VIEW v_rpt_clinic_connectivity_daily IS 'Сводка «ответы РЭМД / ошибки связи» по календарным суткам (UTC) и JID клиники. availability_transport_proxy_pct = успехи / (успехи + сетевые документы) — прокси доступности транспорта, не бизнес-SLA.';

CREATE OR REPLACE VIEW v_rpt_clinic_connectivity_daily_ui AS
SELECT
    v.day_bucket AS "День",
    v.clinic_jid_key AS "JID клиники (ключ)",
    COALESCE(
        dc.jname,
        CASE
            WHEN v.clinic_jid_key ~ '^[0-9]+$' THEN 'Клиника JID: ' || v.clinic_jid_key
            ELSE v.clinic_jid_key
        END
    ) AS "Наименование клиники",
    v.facts_success AS "Ответы РЭМД: успех (документов)",
    v.facts_error_remd AS "Ответы РЭМД: отказ (документов)",
    v.facts_total AS "Ответы РЭМД: всего (документов)",
    v.network_document_errors AS "Ошибки связи (документов)",
    v.availability_transport_proxy_pct AS "Доступность транспорта (прибл.), %"
FROM v_rpt_clinic_connectivity_daily v
LEFT JOIN dim_clinics dc ON dc.jid::text = v.clinic_jid_key AND v.clinic_jid_key ~ '^[0-9]+$';

COMMENT ON VIEW v_rpt_clinic_connectivity_daily_ui IS 'Сводка доступности транспорта по клиникам и дням; см. v_rpt_clinic_connectivity_daily.';

CREATE OR REPLACE VIEW v_rpt_connectivity_global_daily_ui AS
SELECT
    day_bucket AS "День",
    SUM(facts_success)::bigint AS "Успешные ответы РЭМД (документов)",
    SUM(facts_error_remd)::bigint AS "Отказы РЭМД в ответе (документов)",
    SUM(network_document_errors)::bigint AS "Ошибки связи (документов)",
    CASE
        WHEN SUM(facts_success) + SUM(network_document_errors) > 0 THEN
            ROUND(
                100.0 * SUM(facts_success)
                / (SUM(facts_success) + SUM(network_document_errors)),
                2
            )
        ELSE NULL
    END AS "Доступность транспорта (прибл.), %"
FROM v_rpt_clinic_connectivity_daily
GROUP BY day_bucket;

COMMENT ON VIEW v_rpt_connectivity_global_daily_ui IS 'Глобальная сводка по дням: успехи колбэков vs ошибки связи (документы); доля — прокси, не SLA.';
