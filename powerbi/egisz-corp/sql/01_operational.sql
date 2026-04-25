-- Dashboard 01: Оперативный мониторинг (Corp) — см. metabase_dashboards/01_operational.json
-- Источник: public.v_egisz_transactions_enriched_ui

-- Карточка: Топ типов СЭМД
SELECT COALESCE(NULLIF(TRIM("Код СЭМД"), ''), '(пусто)') AS "Код СЭМД",
       COALESCE(NULLIF(TRIM("Наименование СЭМД"), ''), '(пусто)') AS "Наименование СЭМД",
       COUNT(*)::bigint AS "Количество"
FROM public.v_egisz_transactions_enriched_ui
GROUP BY 1, 2
ORDER BY 3 DESC
LIMIT 25;

-- Карточка: По клиникам (имя)
SELECT "JID клиники", "Наименование клиники", COUNT(*)::bigint AS "Количество"
FROM public.v_egisz_transactions_enriched_ui
GROUP BY 1, 2
ORDER BY 3 DESC
LIMIT 20;

-- Карточка: Распределение статусов
SELECT "Статус", COUNT(*)::bigint AS "Количество"
FROM public.v_egisz_transactions_enriched_ui
GROUP BY 1
ORDER BY 2 DESC;

-- Карточка: Последние операции
SELECT *
FROM public.v_egisz_transactions_enriched_ui
ORDER BY "Обработано" DESC NULLS LAST
LIMIT 50;

-- Карточка: Последние ошибки (детализация)
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
