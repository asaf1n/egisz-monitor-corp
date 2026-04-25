-- Dashboard 06: Качество данных (Corp) — metabase_dashboards/06_quality.json

SELECT "JID клиники",
       "Наименование клиники",
       COUNT(*)::bigint AS "Всего отправлено",
       SUM(CASE WHEN "Статус" = 'success' THEN 1 ELSE 0 END)::bigint AS "Успешно",
       SUM(CASE WHEN "Статус" = 'error' THEN 1 ELSE 0 END)::bigint AS "Ошибок",
       ROUND(SUM(CASE WHEN "Статус" = 'success' THEN 1 ELSE 0 END) * 100.0 / NULLIF(COUNT(*), 0), 2) AS "Процент успешных"
FROM public.v_egisz_transactions_enriched_ui
GROUP BY 1, 2
ORDER BY 6 ASC, 3 DESC
LIMIT 50;

SELECT COALESCE(NULLIF(TRIM("Код СЭМД"), ''), '(пусто)') AS "Код СЭМД",
       COALESCE(NULLIF(TRIM("Наименование СЭМД"), ''), '(пусто)') AS "Наименование СЭМД",
       COUNT(*)::bigint AS "Всего отправлено",
       SUM(CASE WHEN "Статус" = 'success' THEN 1 ELSE 0 END)::bigint AS "Успешно",
       SUM(CASE WHEN "Статус" = 'error' THEN 1 ELSE 0 END)::bigint AS "Ошибок",
       ROUND(SUM(CASE WHEN "Статус" = 'success' THEN 1 ELSE 0 END) * 100.0 / NULLIF(COUNT(*), 0), 2) AS "Процент успешных"
FROM public.v_egisz_transactions_enriched_ui
GROUP BY 1, 2
ORDER BY 6 ASC, 3 DESC
LIMIT 50;
