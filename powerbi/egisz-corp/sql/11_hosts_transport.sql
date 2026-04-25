-- Доп. страница: транспортный контур (gost-хост, OID) vs объёмы и ошибки

-- Распределение по токену gost-хоста (топ)
SELECT COALESCE(NULLIF(TRIM("Токен gost-хоста"), ''), '(пусто)') AS "Токен gost-хоста",
       COUNT(*)::bigint AS "Количество"
FROM public.v_egisz_transactions_enriched_ui
GROUP BY 1
ORDER BY 2 DESC
LIMIT 30;

-- Статусы в разрезе токена (матрица для матрицы / stacked bar)
SELECT COALESCE(NULLIF(TRIM("Токен gost-хоста"), ''), '(пусто)') AS "Токен gost-хоста",
       "Статус",
       COUNT(*)::bigint AS "Количество"
FROM public.v_egisz_transactions_enriched_ui
GROUP BY 1, 2
ORDER BY 1, 2;

-- Записи с пустым OID организации при непустом токене (риск сопоставления)
SELECT "Токен gost-хоста",
       "OID организации",
       "JID клиники",
       "Наименование клиники",
       COUNT(*)::bigint AS "Количество"
FROM public.v_egisz_transactions_enriched_ui
WHERE (NULLIF(TRIM("OID организации"), '') IS NULL)
  AND (NULLIF(TRIM("Токен gost-хоста"), '') IS NOT NULL)
GROUP BY 1, 2, 3, 4
ORDER BY 5 DESC
LIMIT 50;
