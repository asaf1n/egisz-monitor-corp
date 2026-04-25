-- Dashboard 02: Сервис интеграции (Corp) — metabase_dashboards/02_service.json

SELECT COALESCE(NULLIF(TRIM("Код СЭМД"), ''), '(пусто)') AS "Код СЭМД",
       COALESCE(NULLIF(TRIM("Наименование СЭМД"), ''), '(пусто)') AS "Наименование СЭМД",
       COUNT(*)::bigint AS "Количество"
FROM public.v_egisz_transactions_enriched_ui
GROUP BY 1, 2
ORDER BY 3 DESC
LIMIT 25;

SELECT "JID клиники", "Наименование клиники", COUNT(*)::bigint AS "Количество"
FROM public.v_egisz_transactions_enriched_ui
GROUP BY 1, 2
ORDER BY 3 DESC
LIMIT 20;
