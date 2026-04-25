-- Доп. страница: сквозная трассировка документа (инциденты, поддержка МИС).
-- В Power BI задайте параметр (например RelatesTo) и подставьте в запрос через Power Query.

-- Шаблон: по correlates (в Power Query подставьте параметр вместо условия 1=0)
SELECT *
FROM public.v_egisz_transactions_enriched_ui
WHERE 1 = 0
ORDER BY "Обработано" DESC NULLS LAST;

-- Шаблон: по localUid СЭМД — аналогично подставьте параметр
SELECT *
FROM public.v_egisz_transactions_enriched_ui
WHERE 1 = 0
ORDER BY "Обработано" DESC NULLS LAST;

-- Лента последних событий с ключами для ручного поиска (последние 7 дней)
SELECT "Связанное сообщение",
       "localUid СЭМД",
       "Статус",
       "Обработано",
       "EMDR ID",
       "Наименование клиники",
       "Код СЭМД",
       "Наименование СЭМД"
FROM public.v_egisz_transactions_enriched_ui
WHERE "Обработано" >= NOW() - INTERVAL '7 days'
ORDER BY "Обработано" DESC NULLS LAST
LIMIT 500;
