# Локальный Kubernetes: терминал и `start.ps1`

Namespace по умолчанию: **`egisz-monitor`**. Контекст: **Docker Desktop Kubernetes** или **kind** (скрипт создаёт кластер `egisz-local`, если нет доступного API).

## Важно: `reset-deploy` vs «чистый образ» conf-ui

- **`apply-rebuild`** (или `apply` с `-DockerNoCache`): пересобирается образ **conf-ui** без кэша Docker, манифесты применяются, поды перезапускаются. **Namespace и PVC Postgres не удаляются** — данные витрины (`egisz_reports` и т.д.) **сохраняются**.
- **`reset-deploy`**: сначала удаляется namespace **`egisz-monitor`** (вместе с StatefulSet Postgres и его **PVC**), затем выполняется полная цепочка как при первом `deploy`. Это **полный greenfield**: все данные в кластерном Postgres для этого namespace **теряются**, если заранее не сделан дамп.

Перед **`reset-deploy`**, если витрина нужна: [scripts/backup-postgres.ps1](../scripts/backup-postgres.ps1), дамп из UI конфигурации (PostgreSQL: бэкап), или вручную `pg_dump` / `kubectl exec` (см. [DOCKER_GORDON_PROMPT.md](DOCKER_GORDON_PROMPT.md)).

Комментарий в `start.ps1` про «витрина не трогается» относится только к тому, что при обычном **`deploy`** / **`reset-deploy`** не выполняется DROP витрины через скрипт — но при **`reset-deploy`** данные витрины **исчезают вместе с удалением namespace**, это не то же самое.

## Быстрая шпаргалка

| Задача | Команда из корня репозитория |
|--------|------------------------------|
| Полный первый подъём стека + сброс БД приложения Metabase | `.\start.ps1 -Action deploy` |
| Чистый namespace + образы без кэша (**PVC Postgres и витрина удаляются**) | `.\start.ps1 -Action reset-deploy` |
| Только пересобрать образы | `.\start.ps1 -Action build` |
| Правки **Config UI (Flask)** → образ + apply манифестов + **перезапуск обоих** web-служб | `.\start.ps1 -Action apply` |
| То же, но **без** перезапуска Metabase (быстрее) | `.\start.ps1 -Action apply -SkipMetabaseRolloutRestart` |
| Config UI: **полная** пересборка образа (`--no-cache`) + apply (**данные Postgres сохраняются**) | `.\start.ps1 -Action apply-rebuild` или `.\scripts\apply-local-rebuild.ps1` |
| Перезапуск **только Metabase** (образ уже в Docker / в кластере) | `.\start.ps1 -Action restart-metabase` |
| Перезапуск **только conf-ui** | `.\start.ps1 -Action restart-conf-ui` |
| Перезапуск **Metabase + conf-ui** без `docker build` и без `kubectl apply` | `.\start.ps1 -Action restart-web` |
| Сброс только app DB Metabase в Postgres | `.\start.ps1 -Action reset-metabase` |
| Статус подов и сервисов | `.\start.ps1 -Action status` |
| Проверка витрины + дашбордов в поде Metabase | `.\start.ps1 -Action verify` |
| Только port-forward (8080 conf-ui, 3000 Metabase) | `.\start.ps1 -Action web` |
| Остановить фоновые port-forward из скрипта | `.\start.ps1 -Action stop-forward` |

Полный список действий: `.\start.ps1 -Action help`.

## Цепочки: что делает скрипт

### `deploy` (по умолчанию)

1. Проверка / создание кластера (**kind** или уже включённый Docker Desktop K8s).  
2. **`build`**: образы **`egisz-conf-ui`** и **`egisz-monitor-metabase`**.  
3. При **kind**: `kind load` образов в кластер.  
4. **`kubectl apply`**: namespace, Postgres, секреты, Metabase, conf-ui; Job схемы витрины; Job БД Airflow.  
5. **DROP/CREATE** базы приложения **`metabase`** в Postgres (чистый Metabase + провижининг дашбордов из образа при старте пода).  
6. **`rollout restart`** Metabase и conf-ui.  
7. Ожидание **Ready**, smoke, **verify** (при сбое — recovery restart по логике `start.ps1`).  
8. Port-forward **8080** и **3000** (если не указан `-SkipPortForwardAfterDeploy`).

### `apply`

1. Только сборка **conf-ui** (Metabase **не** пересобирается).  
2. `kubectl apply` полного стека (как в deploy, **без** DROP/CREATE `metabase`).  
3. По умолчанию **`rollout restart`** и Metabase, и conf-ui → холодный JVM Metabase; для правок только Flask используйте **`-SkipMetabaseRolloutRestart`** — тогда ожидание rollout только у conf-ui.  
4. Smoke, verify, port-forward (опционально).

### `apply-rebuild`

Как **`apply`**, но образ **conf-ui** всегда собирается с **`--no-cache`** (эквивалент `.\start.ps1 -Action apply -DockerNoCache`). Из подкаталога: `.\scripts\apply-local-rebuild.ps1`.

### `reset-deploy`

1. Удаление namespace **`egisz-monitor`** (ожидание, пока ресурсы исчезнут).  
2. **`build`** с **`--no-cache`** для **обоих** образов (conf-ui и Metabase).  
3. Далее как **`deploy`**: загрузка в kind при необходимости, `kubectl apply`, DROP/CREATE БД **`metabase`**, rollout, smoke, **verify**, port-forward.

Итог: **новый PVC Postgres** — витрина пустая до следующего ETL / restore из дампа.

### `restart-metabase` / `restart-conf-ui` / `restart-web`

Только **`kubectl rollout restart`** выбранного deployment (или обоих) и **`kubectl rollout status`** до таймаута. **Не** выполняют `docker build` и **не** применяют YAML. Используйте после ручного `docker build` + загрузки образа в кластер или когда нужно перечитать конфиг из Secret без смены манифеста.

## Терминал: `kubectl` без `start.ps1`

Перейдите в каталог с kubeconfig (обычно уже настроен Docker Desktop / kind).

```powershell
kubectl config current-context
kubectl -n egisz-monitor get pods,svc -o wide
kubectl -n egisz-monitor rollout status deployment/metabase --timeout=600s
kubectl -n egisz-monitor rollout status deployment/conf-ui --timeout=180s
```

Логи:

```powershell
kubectl -n egisz-monitor logs deploy/metabase --tail=100 -f
kubectl -n egisz-monitor logs deploy/conf-ui --tail=80
```

События при **Pending** / **CrashLoop**:

```powershell
kubectl -n egisz-monitor describe pod -l app.kubernetes.io/name=metabase
kubectl -n egisz-monitor get events --sort-by=.lastTimestamp | Select-Object -Last 30
```

Ручной перезапуск (эквивалент actions **`restart-*`**):

```powershell
kubectl -n egisz-monitor rollout restart deployment/metabase
kubectl -n egisz-monitor rollout restart deployment/conf-ui
```

Применить **только** изменённые манифесты (пример):

```powershell
kubectl apply -f k8s/conf-ui.yaml
kubectl apply -f k8s/metabase.yaml
```

**Postgres (StatefulSet)** — рестарт разрывает клиентские соединения; для локальной отладки:

```powershell
kubectl -n egisz-monitor rollout restart statefulset/postgres
kubectl -n egisz-monitor rollout status statefulset/postgres --timeout=600s
```

Выполнить ETL из пода conf-ui:

```powershell
kubectl -n egisz-monitor exec -it deploy/conf-ui -- egisz-monitor sync
```

## Параметры `start.ps1`

| Параметр | Где используется |
|----------|------------------|
| `-SkipKindCluster` | Не создавать kind; должен отвечать уже настроенный `kubectl cluster-info`. |
| `-DockerNoCache` | `build` / `apply`: `docker build --no-cache` для conf-ui (и весь `build` — для Metabase). |
| `-SkipPortForwardAfterDeploy` | `deploy` / `apply` / `reset-deploy`: не поднимать port-forward и не открывать браузер в конце. |
| `-SkipMetabaseRolloutRestart` | Только **`apply`**: не перезапускать Metabase, только conf-ui. |
| `-SkipPostgresPortForward` | `web` / `forward`: не пробрасывать 5432 на хост. |
| `-BackgroundPortForward:$false` | Отдельные окна PowerShell с `kubectl port-forward` вместо фоновых процессов. |

## Проверка после `deploy`, `reset-deploy`, `apply` или `apply-rebuild`

1. **`.\start.ps1 -Action verify`** — проверка витрины и дашбордов Metabase из пода (встроенная логика скрипта уже вызывает её в конце успешного deploy/reset/apply).  
2. При необходимости вручную: **`.\scripts\smoke-post-gordon.ps1`** — краткий снимок `kubectl get` / `top`, запрос **`/healthz`** у conf-ui (нужен доступ к API кластера и при необходимости port-forward на 8080).

## Связанные документы

- **[`METABASE.md`](METABASE.md)** — доступ к UI, провижининг, долгий старт, Pending.  
- **[`SYNC_DIAGNOSTICS.md`](SYNC_DIAGNOSTICS.md)** — сверка ETL и курсора.  
- **[`../k8s/README.md`](../k8s/README.md)** — перечень манифестов и образов.
