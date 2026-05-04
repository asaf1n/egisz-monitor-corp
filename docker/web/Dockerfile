# syntax=docker/dockerfile:1.7
# Multi-stage Config UI: builder ставит wheel'ы пакета + gunicorn в /opt/venv,
# runtime — bookworm-slim с libfbclient2, tini, non-root user. PID 1 — tini, веб-сервер
# — gunicorn (gthread, 1 worker × 16 threads): в одном процессе делим in-memory state
# sync_routes._state, при этом 16 IO-потоков обслуживают опрос UI без блокировок.
# build-args: PYTHON_BASE (default slim-bookworm), PIP_INDEX_URL (PyPI override для private/mirror).
ARG PYTHON_BASE=python:3.11-slim-bookworm
ARG PIP_INDEX_URL=https://pypi.org/simple

# === builder ============================================================
FROM ${PYTHON_BASE} AS builder
ARG PIP_INDEX_URL
ENV PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_INDEX_URL=${PIP_INDEX_URL}
WORKDIR /build
RUN apt-get update \
    && apt-get install -y --no-install-recommends build-essential \
    && rm -rf /var/lib/apt/lists/*
COPY pyproject.toml README.md ./
COPY egisz_monitor_corp ./egisz_monitor_corp
RUN python -m venv /opt/venv \
    && /opt/venv/bin/pip install --upgrade pip \
    && /opt/venv/bin/pip install . "gunicorn>=23.0.0,<24.0.0"

# === runtime ============================================================
FROM ${PYTHON_BASE} AS runtime
ENV LANG=C.UTF-8 \
    LC_ALL=C.UTF-8 \
    PYTHONUTF8=1 \
    PYTHONUNBUFFERED=1 \
    PATH="/opt/venv/bin:${PATH}"

LABEL org.opencontainers.image.title="egisz-conf-ui" \
      org.opencontainers.image.description="Flask Config UI + sync trigger for EGISZ Monitor Corp" \
      org.opencontainers.image.source="https://github.com/your-org/egisz-monitor-corp" \
      org.opencontainers.image.version="0.2.0"

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        ca-certificates curl gnupg libfbclient2 tini \
    && install -d /etc/apt/keyrings \
    && curl -fsSL https://www.postgresql.org/media/keys/ACCC4CF8.asc \
        | gpg --dearmor -o /etc/apt/keyrings/postgresql.gpg \
    && sh -c 'echo "deb [signed-by=/etc/apt/keyrings/postgresql.gpg] http://apt.postgresql.org/pub/repos/apt bookworm-pgdg main" > /etc/apt/sources.list.d/pgdg.list' \
    && apt-get update \
    && apt-get install -y --no-install-recommends postgresql-client-16 \
    && FB_SO="$(find /usr/lib -name 'libfbclient.so.*' -type f 2>/dev/null | sort -V | tail -n1)" \
    && test -n "$FB_SO" && test -f "$FB_SO" \
    && mkdir -p /usr/local/lib \
    && ln -sf "$FB_SO" /usr/local/lib/libfbclient.so \
    && ldconfig \
    && python -c "import ctypes; ctypes.CDLL('/usr/local/lib/libfbclient.so')" \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd -g 10001 app \
    && useradd -m -u 10001 -g 10001 -s /usr/sbin/nologin app

ENV FB_CLIENT_LIBRARY=/usr/local/lib/libfbclient.so

COPY --from=builder /opt/venv /opt/venv
COPY sql ./sql
COPY config/egisz_monitor.example.yaml ./config/egisz_monitor.yaml
# Эталонные JSON дашбордов: резервная выгрузка из Config UI без живого Metabase API (см. metabase_export.build_export_zip_bytes).
COPY metabase_dashboards ./metabase_dashboards

# Финальная проверка драйвера в собранном слое + права на /app/config (туда POST /save
# пишет YAML; в k8s он перекрывается emptyDir с fsGroup=10001, но локально CMD должен работать тоже).
RUN python -c "from firebird.driver import fbapi; fbapi.load_api()" \
    && chown -R app:app /app

ENV EGISZ_MONITOR_CONFIG=/app/config/egisz_monitor.yaml \
    EGISZ_MONITOR_SQL_DIR=/app/sql \
    METABASE_STATIC_DASHBOARDS_DIR=/app/metabase_dashboards

USER 10001:10001
EXPOSE 8080

# tini как PID 1: корректный SIGTERM → gunicorn → graceful drain воркера.
# --timeout 0: отключить «silent worker» kill. Фоновый ETL в отдельном потоке не трогает цикл gunicorn
# (notify/HTTP); при конечном таймауте arbiter перезапускает worker → обрыв sync, UI «Выполняется» без last_stats.
# Перезапуск пода по-прежнему через Kubernetes (readiness/ручной rollout).
ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["gunicorn", \
     "egisz_monitor_corp.config_app:create_app()", \
     "--workers", "1", \
     "--threads", "16", \
     "--worker-class", "gthread", \
     "--timeout", "0", \
     "--graceful-timeout", "120", \
     "--keep-alive", "5", \
     "--worker-tmp-dir", "/dev/shm", \
     "--bind", "0.0.0.0:8080", \
     "--access-logfile", "-", \
     "--error-logfile", "-"]
