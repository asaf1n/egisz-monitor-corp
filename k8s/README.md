# Kubernetes: PostgreSQL и Apache Airflow (egisz-monitor-corp)

Целевой контур: **namespace `egisz-corp`**. Здесь размещаются **витрина PostgreSQL** (данные ETL / Metabase) и **Apache Airflow** (расписание, DAG `egisz_corp_firebird_to_postgres`).

Локальная разработка: `docker-compose.yml` в корне пакета. Манифесты кластера: каталог `k8s/`.

---

## 1. PostgreSQL (`k8s/postgres/`)

### Секрет

```bash
cp k8s/postgres/postgres-secret.example.yaml k8s/postgres/postgres-credentials.yaml
# отредактируйте POSTGRES_PASSWORD и при необходимости POSTGRES_USER / POSTGRES_DB
```

Имя Secret в манифестах: **`postgres-credentials`** (файл на диске можете назвать `postgres-credentials.yaml`).

### Применение

```bash
kubectl apply -f k8s/postgres/namespace.yaml
kubectl apply -f k8s/postgres/postgres-credentials.yaml
kubectl apply -f k8s/postgres/postgres-statefulset.yaml
kubectl apply -f k8s/postgres/postgres-service.yaml
```

Либо `kubectl apply -k k8s/postgres/`: в `kustomization.yaml` должен быть перечислен ваш `postgres-credentials.yaml` в секции `resources` рядом с остальными манифестами.

### Готовность

```bash
kubectl -n egisz-corp rollout status statefulset/postgres
kubectl -n egisz-corp get pods -l app.kubernetes.io/name=postgres
```

### Подключение из кластера

| Поле | Значение |
|------|-----------|
| host | `postgres.egisz-corp.svc.cluster.local` (внутри namespace достаточно `postgres`) |
| port | `5432` |
| database / user / password | из секрета `postgres-credentials` |

### Схема DWH

Из образа или CI с установленным пакетом:

```bash
export EGISZ_CORP_CONFIG=/path/to/egisz_corp.yaml
egisz-corp apply-schema
```

`egisz_corp.yaml` → секция `postgres`: `host` — DNS-имя сервиса PostgreSQL в кластере (например `postgres.egisz-corp.svc.cluster.local`), порт сервиса `5432`.

---

## 2. База метаданных Airflow на том же Postgres

Один инстанс Postgres; служебные таблицы Airflow — в отдельной БД **`airflow`**, витрина ETL — в БД из `postgres-credentials` (например `egisz_reports` или `egisz_corp` по вашей схеме).

```bash
kubectl -n egisz-corp apply -f k8s/postgres/airflow-metadata-init-job.yaml
kubectl -n egisz-corp logs job/airflow-metadata-db-init -f
```

Повтор при уже существующей БД: Job завершится без ошибок (проверка `EXISTS`). Сменить имя Job или удалить старый: `kubectl -n egisz-corp delete job/airflow-metadata-db-init`.

---

## 3. Секрет подключения Airflow → Postgres (метаданные)

Helm chart ожидает Secret с ключом **`connection`** (строка SQLAlchemy).

```bash
cp k8s/airflow/airflow-metadata-secret.example.yaml k8s/airflow/airflow-metadata-secret.yaml
# выставьте тот же пароль, что в postgres-credentials, в строке connection
kubectl apply -f k8s/airflow/airflow-metadata-secret.yaml
```

---

## 4. Образ Airflow с пакетом и DAG

Воркеры выполняют `PythonOperator` и импортируют `egisz_monitor_corp`; DAG лежит в `airflow/dags/`.

```bash
cd egisz-monitor-corp
docker build -f k8s/airflow/Dockerfile -t <registry>/egisz-corp-airflow:2.9.3 .
docker push <registry>/egisz-corp-airflow:2.9.3
```

При необходимости установите клиент Firebird в образе (см. комментарии в `k8s/airflow/Dockerfile`).

В `k8s/airflow/values-corp.example.yaml` укажите `images.airflow.repository` и `images.airflow.tag` на собранный образ.

---

## 5. Конфиг ETL для подов Airflow (рекомендуется)

Не храните пароли Firebird только в образе. Создайте Secret из файла:

```bash
kubectl -n egisz-corp create secret generic egisz-corp-app-config \
  --from-file=egisz_corp.yaml=./config/egisz_corp.yaml \
  --dry-run=client -o yaml | kubectl apply -f -
```

Добавьте в Helm values (фрагмент) монтирование и переменную окружения для **Celery workers** (и при необходимости для других компонентов, где исполняются таски):

```yaml
workers:
  celery:
    extraVolumes:
      - name: egisz-corp-config
        secret:
          secretName: egisz-corp-app-config
    extraVolumeMounts:
      - name: egisz-corp-config
        mountPath: /opt/egisz-monitor-corp/config/egisz_corp.yaml
        subPath: egisz_corp.yaml
        readOnly: true
    env:
      - name: EGISZ_CORP_CONFIG
        value: /opt/egisz-monitor-corp/config/egisz_corp.yaml
```

Пути в DAG по умолчанию: `EGISZ_CORP_PROJECT_ROOT=/opt/egisz-monitor-corp`, конфиг — `egisz_corp.yaml` внутри образа; Secret смонтированный поверх перекрывает файл из образа.

---

## 6. Установка Airflow (Helm)

```bash
helm repo add apache-airflow https://airflow.apache.org/charts
helm repo update
helm upgrade --install airflow apache-airflow/airflow \
  --namespace egisz-corp \
  -f egisz-monitor-corp/k8s/airflow/values-corp.example.yaml
```

Зафиксируйте версию chart для продакшена, например: `--version 1.18.0` (подберите совместимую с вашим кластером).

### UI без Ingress

```bash
kubectl -n egisz-corp port-forward svc/airflow-webserver 8080:8080
```

Учётная запись из `createUserJob.defaultUser` в values (смените пароль).

### DAG

Файл репозитория: `airflow/dags/egisz_corp_etl_dag.py`. После сборки образа он копируется в `/opt/airflow/dags/`. Расписание: переменная окружения `EGISZ_CORP_AIRFLOW_SCHEDULE` (в values через `env`) или правка DAG.

---

## 7. Metabase

Metabase в репозитории: манифест `k8s/metabase.yaml`, описание полей витрины — `docs/METABASE.md`. Подключение Metabase к PostgreSQL в кластере: хост `postgres` (или FQDN сервиса), порт `5432`, имя БД из секрета `postgres-credentials`.

---

## 8. Port-forward с рабочей машины

```bash
kubectl -n egisz-corp port-forward svc/postgres 5432:5432
```

Команда пробрасывает порт сервиса `postgres` на `localhost:5432` на машине, где запущен `kubectl`.

### Config UI и Metabase (localhost)

В `k8s/conf-ui.yaml` и `k8s/metabase.yaml` сервисы объявлены как **NodePort**: `8080:30808/TCP`, `3000:30300/TCP` (см. `kubectl -n egisz-corp get svc`). Для доступа с Windows через **стандартные порты localhost** (`5432`, `8080`, `3000`) без ручного набора NodePort:

```powershell
.\start.ps1 -Action web
# то же самое:
.\start.ps1 -Action forward
```

Скрипт запускает три окна PowerShell с `kubectl port-forward` (сервисы `postgres`, `conf-ui`, `metabase`) и открывает браузер на `http://127.0.0.1:8080/` и `http://127.0.0.1:3000/`. Окна держите открытыми на время работы. Если порт `5432` на ПК занят другим процессом: `.\start.ps1 -Action web -SkipPostgresPortForward`.

### Firebird с хоста Windows в под Config UI

Параметры по умолчанию: `k8s/local/egisz_corp.yaml` — **`host.docker.internal`**, порт **`3050`**.

Поле **database** в форме DSN — строка в формате Firebird для **того процесса `firebird`, к которому выполняется TCP-подключение**: зарегистрированный alias (например `proxy_egisz`) или путь к `.fdb` **на файловой системе, видимой этому серверу** (как в DBeaver при том же `host`/`port`).

Образ `egisz-conf-ui` собирается с **`libfbclient`** (`docker/web/Dockerfile`, переменная **`FB_CLIENT_LIBRARY`** в `k8s/conf-ui.yaml`). После правок Dockerfile: `.\start.ps1 -Action deploy` или последовательность `build` + `apply` и обновление пода `conf-ui`.
