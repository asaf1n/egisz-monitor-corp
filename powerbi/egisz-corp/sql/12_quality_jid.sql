-- Доп. страница: риски качества сопоставлений JID / справочники

-- Топ «Клиника JID: …» (плейсхолдер имени в витрине при отсутствии в справочнике)
SELECT "JID клиники",
       "Наименование клиники",
       COUNT(*)::bigint AS "Количество"
FROM public.v_egisz_transactions_enriched_ui
WHERE "Наименование клиники" LIKE 'Клиника JID:%'
GROUP BY 1, 2
ORDER BY 3 DESC
LIMIT 50;

-- Записи без JID клиники (текстовое поле пустое или нечисловой маркер — в UI JID как text)
SELECT COUNT(*)::bigint AS "Строк без JID"
FROM public.v_egisz_transactions_enriched_ui
WHERE NULLIF(TRIM("JID клиники"), '') IS NULL;

-- Связь объёма parse errors с последними сутками (индикатор проблем разбора)
SELECT DATE(created_at) AS "Дата",
       COUNT(*)::bigint AS "Ошибок парсинга"
FROM public.stg_parse_errors
WHERE created_at >= NOW() - INTERVAL '30 days'
GROUP BY 1
ORDER BY 1 DESC;
