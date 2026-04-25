-- Dashboard 07: Глубокий анализ ошибок (Corp) — metabase_dashboards/07_errors_deep.json

SELECT LEFT(err->>'message', 150) AS "Текст ошибки",
       COUNT(*)::bigint AS "Количество"
FROM public.v_egisz_transactions_enriched_ui,
     jsonb_array_elements("Ошибки JSON") AS e(err)
WHERE "Статус" = 'error'
  AND NULLIF(err->>'message', '') IS NOT NULL
GROUP BY 1
ORDER BY 2 DESC
LIMIT 20;

SELECT "JID клиники",
       "Наименование клиники",
       COALESCE(NULLIF(TRIM("Код СЭМД"), ''), '(пусто)') AS "Код СЭМД",
       COALESCE(NULLIF(TRIM("Наименование СЭМД"), ''), '(пусто)') AS "Наименование СЭМД",
       LEFT(err->>'message', 100) AS "Текст ошибки",
       COUNT(*)::bigint AS "Количество"
FROM public.v_egisz_transactions_enriched_ui,
     jsonb_array_elements("Ошибки JSON") AS e(err)
WHERE "Статус" = 'error'
  AND NULLIF(err->>'message', '') IS NOT NULL
GROUP BY 1, 2, 3, 4, 5
ORDER BY 6 DESC
LIMIT 50;
