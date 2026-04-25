-- Dashboard 08: Аналитика зависших документов (Corp) — metabase_dashboards/08_pending_agg.json
-- Источник: public.v_rpt_documents_no_response_ui

-- Топ клиник с зависшими документами
SELECT "JID клиники", "Наименование клиники", COUNT(*)::bigint AS "Количество"
FROM public.v_rpt_documents_no_response_ui
GROUP BY 1, 2
ORDER BY 3 DESC
LIMIT 20;

-- Возраст зависших документов
SELECT CASE
           WHEN EXTRACT(EPOCH FROM (NOW() - "Отправлено")) / 3600 < 1 THEN '< 1 часа'
           WHEN EXTRACT(EPOCH FROM (NOW() - "Отправлено")) / 3600 < 12 THEN '1-12 часов'
           WHEN EXTRACT(EPOCH FROM (NOW() - "Отправлено")) / 3600 < 24 THEN '12-24 часа'
           ELSE '> 24 часов'
       END AS "Время ожидания",
       COUNT(*)::bigint AS "Количество"
FROM public.v_rpt_documents_no_response_ui
GROUP BY 1
ORDER BY 1;

-- Зависшие документы по типам СЭМД
SELECT COALESCE(NULLIF(TRIM("Код СЭМД"), ''), '(пусто)') AS "Код СЭМД",
       COALESCE(NULLIF(TRIM("Наименование СЭМД"), ''), '(пусто)') AS "Наименование СЭМД",
       COUNT(*)::bigint AS "Количество"
FROM public.v_rpt_documents_no_response_ui
GROUP BY 1, 2
ORDER BY 3 DESC
LIMIT 25;
