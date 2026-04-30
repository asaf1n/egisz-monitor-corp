# OSS: тег + digest = один и тот же бинарь на всех хостах. Следующий bump: поднять тег, пересобрать,
# подставить digest из вывода docker build. Hub: https://hub.docker.com/r/metabase/metabase/tags?name=v0.60.2
FROM metabase/metabase:v0.60.2.5@sha256:e6fb150fad339ae05d4b77c31de077fa3ff62e894006bb850172eb1df2a40fb6

ENV LANG=C.UTF-8 LC_ALL=C.UTF-8

# build-deps как virtual: apk index, кеш и .apk файлы убираются в одном RUN — не остаются в финальном слое.
# tini не ставим: базовый metabase/metabase:* уже использует /app/run_metabase.sh + JRE wrapper, кастомный
# entrypoint.sh ловит SIGTERM и пробрасывает в JVM. Минимум для provision/verify: jq, curl, bash, psql.
# Virtual package set: apk index, кеш и .apk-файлы убираются в одном RUN — не остаются в финальном
# слое. tini не ставим: базовый metabase/metabase:* уже использует /app/run_metabase.sh + JRE wrapper,
# кастомный entrypoint.sh ловит SIGTERM. Минимум для provision/verify: jq, curl, bash, psql.
# В базовом образе нет user metabase — он запускается под root и сам не делает privilege drop.
# Создаём UID 1500 «metabase» с правами на /app и /tmp; HEALTHCHECK + non-root.
RUN apk add --no-cache --virtual .corp-runtime-deps jq curl postgresql-client bash shadow \
    && groupadd -g 1500 metabase \
    && useradd -u 1500 -g 1500 -m -d /home/metabase -s /sbin/nologin metabase \
    && chown -R metabase:metabase /app /tmp \
    && apk del shadow \
    && rm -rf /var/cache/apk/*

# Скрипты слоем выше — правки JSON дашбордов не инвалидируют образ скриптов (быстрая итерация поверх «11_*.json»).
COPY --chown=metabase:metabase metabase/provision.sh /app/provision.sh
COPY --chown=metabase:metabase metabase/setup-dashboards.sh /app/setup-dashboards.sh
COPY --chown=metabase:metabase metabase/verify-corp-stack.sh /app/verify-corp-stack.sh
COPY --chown=metabase:metabase metabase/smoke-metabase-ui.sh /app/smoke-metabase-ui.sh
COPY --chown=metabase:metabase metabase/entrypoint.sh /app/entrypoint.sh
RUN chmod +x /app/provision.sh /app/setup-dashboards.sh /app/verify-corp-stack.sh \
              /app/smoke-metabase-ui.sh /app/entrypoint.sh

# Дашборды копируются ПОСЛЕ скриптов — изменение JSON не пересобирает слой со скриптами.
COPY --chown=metabase:metabase metabase_dashboards/ /app/metabase_dashboards/
# Любое изменение JSON инвалидирует следующий слой через checksum-фолловер — иначе Docker мог отдать
# кэш без обновлённых дашбордов при дубликате имени слоя.
RUN find /app/metabase_dashboards -maxdepth 1 -name '*.json' -type f -exec sha256sum {} \; \
      | sha256sum > /app/metabase_dashboards/.manifest.sha256 \
    && chmod -R a+r /app/metabase_dashboards/

USER 1500:1500

# Liveness в k8s также использует /api/health, но HEALTHCHECK помогает локальному `docker run`,
# docker compose и smoke-test'ам без полного k8s-стека.
HEALTHCHECK --interval=30s --timeout=5s --start-period=120s --retries=3 \
    CMD curl --fail --silent --output /dev/null http://localhost:3000/api/health || exit 1

ENTRYPOINT ["/bin/bash", "/app/entrypoint.sh"]
