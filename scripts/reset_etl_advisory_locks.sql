-- Разовая диагностика/сброс: завершить сессии, удержавшие advisory lock на текущей БД.
-- Выполняйте от суперпользователя или с правом сигналить чужие бэкенды.
-- Аналог CLI: egisz-monitor reset-etl-locks

SELECT pid, usename, application_name, state, query
FROM pg_stat_activity
WHERE pid IN (
  SELECT l.pid
  FROM pg_locks l
  WHERE l.locktype = 'advisory'
    AND l.granted
    AND l.pid IS NOT NULL
)
  AND datname = current_database()
  AND pid <> pg_backend_pid()
ORDER BY pid;

-- Раскомментируйте для принудительного сброса:
-- SELECT pg_terminate_backend(pid) FROM ... (см. egisz_monitor_corp.pg_warehouse.terminate_other_sessions_with_advisory_locks)
