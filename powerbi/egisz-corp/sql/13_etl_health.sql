-- Доп. страница: здоровье ETL (не бизнес-время; см. docs/METABASE.md про etl_state)

SELECT pipeline AS "Пайплайн",
       last_log_id AS "Последний LOGID Firebird",
       updated_at AS "Обновлено"
FROM public.etl_state
ORDER BY pipeline;
