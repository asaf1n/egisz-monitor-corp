-- Dashboard 09: Управленческий дашборд (Corp) — metabase_dashboards/09_executive.json

-- Всего обработано за сегодня
SELECT COUNT(*)::bigint AS "Количество"
FROM public.v_egisz_transactions_enriched_ui
WHERE DATE("Обработано") = CURRENT_DATE;

-- Успешно за сегодня
SELECT COUNT(*)::bigint AS "Количество"
FROM public.v_egisz_transactions_enriched_ui
WHERE DATE("Обработано") = CURRENT_DATE
  AND "Статус" = 'success';

-- % ошибок за сегодня
SELECT ROUND(SUM(CASE WHEN "Статус" = 'error' THEN 1 ELSE 0 END) * 100.0 / NULLIF(COUNT(*), 0), 2) AS "Процент ошибок"
FROM public.v_egisz_transactions_enriched_ui
WHERE DATE("Обработано") = CURRENT_DATE;

-- Документов в ожидании (без ответа)
SELECT COUNT(*)::bigint AS "Количество"
FROM public.v_rpt_documents_no_response_ui;
