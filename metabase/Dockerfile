# OSS: тег + digest = воспроизводимость. Bump: docker pull metabase/metabase:v0.60.2.X → docker build → 
# docker inspect <built-image> → скопировать digest из RepoDigests[0].
# Renovate/ручное отслеживание: https://hub.docker.com/r/metabase/metabase/tags
FROM metabase/metabase:v0.60.2.5@sha256:e6fb150fad339ae05d4b77c31de077fa3ff62e894006bb850172eb1df2a40fb6

LABEL org.opencontainers.image.title="egisz-metabase"
LABEL org.opencontainers.image.description="Metabase OSS with EGISZ dashboards, provision scripts, non-root UID 1500"
LABEL org.opencontainers.image.source="https://github.com/your-org/egisz-monitor-corp"
LABEL org.opencontainers.image.version="0.2.0"

ENV LANG=C.UTF-8 LC_ALL=C.UTF-8

# Virtual apk deps в одном RUN — индекс/кеш не остаются в финальном слое. Базовый образ уже даёт run_metabase.sh + JRE;
# кастомный entrypoint.sh ловит SIGTERM. Для provision/verify: jq, curl, bash, psql; для выгрузки JSON — python3 (модуль egisz_monitor_corp.metabase_export).
# В базовом образе нет user metabase — он запускается под root и сам не делает privilege drop.
# Создаём UID 1500 «metabase» с правами на /app и /tmp; HEALTHCHECK + non-root.
RUN apk add --no-cache --virtual .corp-runtime-deps jq curl postgresql-client bash shadow python3 \
    && groupadd -g 1500 metabase \
    && useradd -u 1500 -g 1500 -m -d /home/metabase -s /sbin/nologin metabase \
    && chown -R metabase:metabase /app /tmp \
    && apk del shadow \
    && rm -rf /var/cache/apk/*

# Скрипты слоем — правки JSON дашбордов не инвалидируют образ скриптов (быстрая итерация по `metabase_dashboards/*.json`).
# Порядок: provision/verify/smoke → export → dashboards. На каждый слой свой checksum.
COPY --chown=metabase:metabase metabase/include/mb_list.sh /app/include/mb_list.sh
COPY --chown=metabase:metabase metabase/provision.sh /app/provision.sh
COPY --chown=metabase:metabase metabase/setup-dashboards.sh /app/setup-dashboards.sh
COPY --chown=metabase:metabase metabase/verify-corp-stack.sh /app/verify-corp-stack.sh
COPY --chown=metabase:metabase metabase/smoke-metabase-ui.sh /app/smoke-metabase-ui.sh
COPY --chown=metabase:metabase metabase/entrypoint.sh /app/entrypoint.sh
COPY --chown=metabase:metabase egisz_monitor_corp/metabase_export.py /app/egisz_monitor_corp/metabase_export.py
RUN printf '%s\n' '"""Package stub for Metabase export CLI (PYTHONPATH=/app)."""' > /app/egisz_monitor_corp/__init__.py \
    && chown metabase:metabase /app/egisz_monitor_corp/__init__.py
RUN chmod +x /app/provision.sh /app/setup-dashboards.sh /app/verify-corp-stack.sh \
              /app/smoke-metabase-ui.sh /app/entrypoint.sh /app/include/mb_list.sh

# Дашборды копируются ОТДЕЛЬНЫМ слоем — изменение JSON не пересобирает слой со скриптами.
COPY --chown=metabase:metabase metabase_dashboards/ /app/metabase_dashboards/
# Checksum манифеста дешбордов (порядок файлов — sort; как corp_mb_compute_dashboards_bundle_sha в provision.sh).
RUN find /app/metabase_dashboards -maxdepth 1 -name '*.json' -type f | LC_ALL=C sort | xargs sha256sum \
      | sha256sum > /app/metabase_dashboards/.manifest.sha256 \
    && chmod -R a+r /app/metabase_dashboards/

USER 1500:1500

# Liveness в k8s также использует /api/health, но HEALTHCHECK помогает локальному `docker run`,
# docker compose и smoke-test'ам без полного k8s-стека.
HEALTHCHECK --interval=30s --timeout=5s --start-period=120s --retries=3 \
    CMD curl --fail --silent --output /dev/null http://localhost:3000/api/health || exit 1

ENTRYPOINT ["/bin/bash", "/app/entrypoint.sh"]
