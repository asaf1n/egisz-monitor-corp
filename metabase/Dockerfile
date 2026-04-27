# OSS: тег + digest = один и тот же бинарь на всех хостах. Следующий bump: поднять тег, пересобрать, подставить digest из вывода docker build.
# Hub: https://hub.docker.com/r/metabase/metabase/tags?name=v0.60.2
FROM metabase/metabase:v0.60.2.5@sha256:e6fb150fad339ae05d4b77c31de077fa3ff62e894006bb850172eb1df2a40fb6
ENV LANG=C.UTF-8 LC_ALL=C.UTF-8
RUN apk add --no-cache jq curl postgresql-client bash
COPY metabase/provision.sh /app/provision.sh
COPY metabase/setup-dashboards.sh /app/setup-dashboards.sh
COPY metabase/verify-corp-stack.sh /app/verify-corp-stack.sh
COPY metabase/smoke-metabase-ui.sh /app/smoke-metabase-ui.sh
COPY metabase_dashboards/ /app/metabase_dashboards/
# Любое изменение JSON инвалидирует слой — иначе Docker мог отдать кэш без обновлённых дашбордов.
RUN find /app/metabase_dashboards -maxdepth 1 -name '*.json' -type f -exec sha256sum {} \; | sha256sum > /app/metabase_dashboards/.manifest.sha256
COPY metabase/entrypoint.sh /app/entrypoint.sh
RUN chmod +x /app/provision.sh /app/setup-dashboards.sh /app/verify-corp-stack.sh /app/smoke-metabase-ui.sh /app/entrypoint.sh && chmod -R a+r /app/metabase_dashboards/
ENTRYPOINT ["/bin/bash", "/app/entrypoint.sh"]
