# Образ Airflow с пакетом egisz-monitor-corp и DAG (Celery workers выполняют ETL).
# Сборка из каталога egisz-monitor-corp:
#   docker build -f k8s/airflow/Dockerfile -t <registry>/egisz-corp-airflow:2.9.3 .
#
FROM apache/airflow:2.9.3-python3.11
USER root
COPY . /opt/egisz-monitor-corp
RUN pip install --no-cache-dir -e /opt/egisz-monitor-corp
# Раскомментируйте при необходимости клиента Firebird для firebird-driver на воркерах:
# RUN apt-get update \
#     && apt-get install -y --no-install-recommends firebird3.0-common libfbclient2 \
#     && rm -rf /var/lib/apt/lists/*
USER airflow
COPY airflow/dags/*.py /opt/airflow/dags/
ENV EGISZ_CORP_PROJECT_ROOT=/opt/egisz-monitor-corp
