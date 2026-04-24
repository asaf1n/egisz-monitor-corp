FROM python:3.11-slim
WORKDIR /app
# При необходимости клиента Firebird для firebird-driver: см. README (volume libfbclient или образ на базе Debian + firebird3.0-client).
COPY pyproject.toml README.md ./
COPY egisz_monitor_corp ./egisz_monitor_corp
COPY sql ./sql
COPY config/egisz_corp.example.yaml ./config/egisz_corp.yaml
RUN pip install --no-cache-dir .
ENV EGISZ_CORP_CONFIG=/app/config/egisz_corp.yaml
ENV FLASK_APP=egisz_monitor_corp.config_app:create_app
ENV PYTHONUNBUFFERED=1
EXPOSE 8080
CMD ["python", "-m", "flask", "run", "--host", "0.0.0.0", "--port", "8080"]
