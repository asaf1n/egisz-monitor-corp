# Локальный Kubernetes: терминал и `start.ps1`

Namespace по умолчанию: **`egisz-monitor`**. Контекст: **Docker Desktop Kubernetes** или **kind** (скрипт создаёт кластер `egisz-local`, если нет доступного API).

## Быстрая шпаргалка

| Задача | Команда из корня репозитория |
|--------|------------------------------|
| Полный первый подъём стека + сброс БД приложения Metabase | `.\start.ps1 -Action deploy` |
| Чистый namespace + образы без кэша | `.\start.ps1 -Action reset-deploy` |
| Только пересобрать образы | `.\start.ps1 -Action build` |
| Правки **Config UI (Flask)** → образ + apply манифестов + **перезапуск обоих** web-служб | `.\start.ps1 -Action apply` |
| То же, но **без** перезапуска Metabase (быстрее) | `.\start.ps1 -Action apply -SkipMetabaseRolloutRestart` |
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

## Связанные документы

- **[`METABASE.md`](METABASE.md)** — доступ к UI, провижининг, долгий старт, Pending.  
- **[`SYNC_DIAGNOSTICS.md`](SYNC_DIAGNOSTICS.md)** — сверка ETL и курсора.  
- **[`../k8s/README.md`](../k8s/README.md)** — перечень манифестов и образов.
