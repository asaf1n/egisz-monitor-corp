-- Dashboard 03: Ошибки и разбор (Corp) — metabase_dashboards/03_errors.json

SELECT id AS "Номер записи",
       error_code AS "Код ошибки",
       LEFT(message, 200) AS "Сообщение",
       created_at AS "Создано",
       relates_to_id AS "Связанное сообщение"
FROM public.stg_parse_errors
ORDER BY id DESC
LIMIT 100;

SELECT "localUid СЭМД",
       "JID клиники",
       "Токен gost-хоста",
       "OID организации",
       "Код СЭМД",
       "Наименование СЭМД",
       "Наименование клиники",
       "ИНН клиники",
       "OID клиники",
       "EMDR ID",
       "Дата регистрации",
       (SELECT LEFT(string_agg(NULLIF(err->>'message', ''), '; ' ORDER BY ord), 500)
        FROM jsonb_array_elements("Ошибки JSON") WITH ORDINALITY AS e(err, ord)
        WHERE NULLIF(err->>'message', '') IS NOT NULL) AS "Текст ошибки",
       "Обработано",
       "Связанное сообщение"
FROM public.v_egisz_transactions_enriched_ui
WHERE "Статус" = 'error'
ORDER BY "Обработано" DESC NULLS LAST
LIMIT 50;
