-- Dashboard 05: Динамика и тренды (Corp) — metabase_dashboards/05_trends.json

SELECT DATE("Обработано") AS "Дата",
       "Статус",
       COUNT(*)::bigint AS "Количество"
FROM public.v_egisz_transactions_enriched_ui
GROUP BY 1, 2
ORDER BY 1 ASC;

SELECT DATE("Обработано") AS "Дата",
       COALESCE(NULLIF(TRIM("Код СЭМД"), ''), '(пусто)') AS "Код СЭМД",
       COALESCE(NULLIF(TRIM("Наименование СЭМД"), ''), '(пусто)') AS "Наименование СЭМД",
       COUNT(*)::bigint AS "Количество"
FROM public.v_egisz_transactions_enriched_ui
GROUP BY 1, 2, 3
ORDER BY 1 ASC;
