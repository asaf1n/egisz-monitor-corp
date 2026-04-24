FROM metabase/metabase:latest
RUN apk add --no-cache jq curl postgresql-client bash
COPY metabase/provision.sh /app/provision.sh
COPY metabase/setup-dashboards.sh /app/setup-dashboards.sh
COPY metabase_dashboards/ /app/metabase_dashboards/
COPY metabase/entrypoint.sh /app/entrypoint.sh
RUN chmod +x /app/provision.sh /app/setup-dashboards.sh /app/entrypoint.sh && chmod -R a+r /app/metabase_dashboards/
ENTRYPOINT ["/bin/bash", "/app/entrypoint.sh"]
