# Kubernetes (namespace `egisz-monitor`)

Локальный стек поднимается из корня через **`.\start.ps1`** (см. [`docs/KUBERNETES_LOCAL.md`](../docs/KUBERNETES_LOCAL.md)).

## Манифесты в корне `k8s/`

| Файл | Назначение |
|------|------------|
| `metabase.yaml` | Service **`metabase`** (LoadBalancer :3000), Deployment **`metabase`**, образ **`egisz-monitor-metabase`** (тег в YAML) |
| `conf-ui.yaml` | Service **`conf-ui`** (LoadBalancer :8080), PDB, Deployment **`conf-ui`**, образ **`egisz-conf-ui:corp-web`** |
| `etl-cron.yaml` | CronJob **`egisz-monitor-sync`** (`egisz-monitor sync`), Secret **`egisz-monitor-conf-ui-config`** |
| `metabase-admin-secret.example.yaml` | Шаблон Secret **`metabase-admin`**; рабочая копия создаётся скриптом как `k8s/metabase-admin-secret.yaml` (не коммитить прод-секреты) |

## `k8s/postgres/`

StatefulSet Postgres, Service **`postgres`** (ClusterIP + **NodePort 30432** на хост), Job’ы схемы витрины и БД Metabase/Airflow. Примеры: `postgres-secret.example.yaml`.

## `k8s/local/`

Пример фрагмента конфига для Secret Config UI: `egisz_monitor.yaml`.

## `k8s/airflow/`

Helm values и образ для опционального Airflow — см. [`k8s/airflow/README.md`](airflow/README.md).

## Сервисы и доступ

| Service (DNS в кластере) | Тип | С хоста (типично) |
|--------------------------|-----|-------------------|
| `postgres.egisz-monitor.svc.cluster.local:5432` | NodePort | `127.0.0.1:30432` |
| `metabase:3000` | LoadBalancer | `http://127.0.0.1:3000` (Docker Desktop) или `kubectl port-forward svc/metabase 3000:3000` |
| `conf-ui:8080` | LoadBalancer | `http://127.0.0.1:8080` или `kubectl port-forward svc/conf-ui 8080:8080` |

Имена Service совпадают с именами Deployment (**`metabase`**, **`conf-ui`**, **`postgres`**).
