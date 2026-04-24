# bookworm: стабильные пути libfbclient2; slim — меньше слоя.
FROM python:3.11-slim-bookworm
WORKDIR /app

# firebird-driver грузит libfbclient (env FB_CLIENT_LIBRARY). Реальный ELF лежит как libfbclient.so.X.Y.Z под triplet.
RUN apt-get update \
    && apt-get install -y --no-install-recommends libfbclient2 \
    && FB_SO="$(find /usr/lib -name 'libfbclient.so.*' -type f 2>/dev/null | sort -V | tail -n1)" \
    && test -n "$FB_SO" && test -f "$FB_SO" \
    && mkdir -p /usr/local/lib \
    && ln -sf "$FB_SO" /usr/local/lib/libfbclient.so \
    && ldconfig \
    && python -c "import ctypes; ctypes.CDLL('/usr/local/lib/libfbclient.so')" \
    && rm -rf /var/lib/apt/lists/*

ENV FB_CLIENT_LIBRARY=/usr/local/lib/libfbclient.so

COPY pyproject.toml README.md ./
COPY egisz_monitor_corp ./egisz_monitor_corp
COPY sql ./sql
COPY config/egisz_corp.example.yaml ./config/egisz_corp.yaml
RUN pip install --no-cache-dir . \
    && rm -rf /app/egisz_monitor_corp \
    && FB_CLIENT_LIBRARY=/usr/local/lib/libfbclient.so python -c "from firebird.driver import fbapi; fbapi.load_api()"
ENV EGISZ_CORP_CONFIG=/app/config/egisz_corp.yaml
ENV EGISZ_CORP_SQL_DIR=/app/sql
ENV FLASK_APP=egisz_monitor_corp.config_app:create_app
ENV PYTHONUNBUFFERED=1
EXPOSE 8080
CMD ["python", "-m", "flask", "run", "--host", "0.0.0.0", "--port", "8080"]
