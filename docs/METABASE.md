# Metabase на корпоративном DWH

**Доступ с ПК (порт 3000):** в `k8s/metabase.yaml` объявлен Service **`metabase-lb`** (`type: LoadBalancer`), на Docker Desktop он обычно публикует **[http://127.0.0.1:3000/](http://127.0.0.1:3000/)** — без `kubectl port-forward`. Старый `NodePort` **30300** остаётся запасным. Если `LoadBalancer` в Pending (не Docker Desktop) — `kubectl -n egisz-corp port-forward svc/metabase 3000:3000` или NodePort. В Deployment задано **`MB_SITE_URL=http://127.0.0.1:3000`** (совпадение с тем, как открываете в браузере, см. раздел «Главная Metabase» ниже).

**Где искать дашборды:** провижининг кладёт дашборды **в корень личной коллекции** пользователя Metabase, под которым идёт API-сессия (обычно первый администратор): `GET /api/user/current` → `personal_collection_id`. В UI это тот же пункт **«Персональная коллекция …»** / **Your personal collection** — откройте его: дашборды должны быть **на этой странице**, без отдельной вложенной папки. Проверка после деплоя: `.\start.ps1 -Action verify` или логи пода `metabase` (`provision.sh`, `verify-corp-stack.sh`).

### Первичный набор отчётов: только `metabase_dashboards/*.json`

**Источник отчётов** — только JSON в репозитории: один файл = один дашборд, native-вопросы **вложены** в JSON и создаются `setup-dashboards.sh` при провижининге, не через «Сохранить вопрос» в UI. Список: [`metabase_dashboards/README.md`](../metabase_dashboards/README.md).

- **Пустой Metabase:** после `POST /api/setup` (первый админ) и появления схемы витрины в Postgres `metabase/provision.sh` вызывает `setup-dashboards.sh`, который **с нуля** создаёт весь набор. Повторный прогон **идемпотентен** для одноимённых дашбордов/карточек в той же персональной коллекции: скрипт удаляет устаревшие сущности по списку из `*.json` и снова импортирует файлы.
- **Изменение SQL или состава карт:** правьте JSON в `metabase_dashboards/`, **пересоберите** образ `egisz-corp-metabase` (в каталог копируется весь `metabase_dashboards/`, см. `metabase/Dockerfile`) и **перезапустите** deployment Metabase — `entrypoint` снова выполнит `provision.sh`. Без обновлённого образа под **не** увидит новые JSON.
- **Только API на localhost, без k8s:** `.\metabase\provision-local.ps1` — собирает образ и запускает `setup-dashboards.sh` с **монтированием** текущего `metabase_dashboards` с диска, так что дашборды в Metabase соответствуют рабочей копии репозитория, даже до коммита в образ.

**Учётная запись администратора Metabase:** `admin@egisz.local` / `egisz`. В Kubernetes значения приходят из Secret `metabase-admin` (`METABASE_ADMIN_EMAIL`, `METABASE_ADMIN_PASSWORD` в `k8s/metabase.yaml`); шаблон — `k8s/metabase-admin-secret.example.yaml`. Локальный `.\start.ps1` при записи секретов использует тот же набор. Для `.\metabase\provision-local.ps1` задайте те же переменные или передайте `-AdminEmail` / `-AdminPassword`.

### Главная Metabase: «Приветствую …» и «Произошла ошибка»

Текст на **домашней** странице (`/`) идёт из Metabase; он **не** из этого репозитория. Блок с ошибкой значит, что один из запросов виджета главной (активность, закреплённое, «продолжить») вернул сбой — витрина и дашборды в коллекции при этом часто **работают**.

Что сделать по шагам:

1. **Открыть дашборды минуя главную:** сайдбар → **«Персональная коллекция …»** → дашборд (например **«Управленческий дашборд»**). Либо вручную URL вида `/collection/<personal_collection_id>` (id смотрите в выводе verify или в логах провижининга).
2. **Site URL:** **Admin (шестерёнка) → Administration → Settings → General → Site URL** — должен совпадать с тем, как вы реально открываете Metabase (одинаковый хост и порт: `http://127.0.0.1:3000` и `http://localhost:3000` для браузера — это **разные** origin). После смены перезагрузите страницу. В k8s при постоянном способе доступа можно задать переменную **`MB_SITE_URL`** в Deployment (см. комментарий в `k8s/metabase.yaml`).
3. **После пересоздания карточек провижингом** старые **закладки** или закрепление на главной могли указывать на несуществующие id: снимите закладки с «битых» вопросов/дашбордов или откройте Metabase в **режиме инкогнито** и зайдите снова.
4. **Диагностика:** в браузере **DevTools → Network** обновите `/` и найдите запрос к `/api/...` со статусом 4xx/5xx; на сервере: `kubectl -n egisz-corp logs deploy/metabase --tail=200`.

## Подключение

1. В Metabase: **Admin → Databases → Add database → PostgreSQL**.
2. Используйте те же параметры, что в `config/egisz_corp.yaml` → секция `postgres`.
3. Схема по умолчанию: `public` (или значение `postgres.schema` в YAML).

## Кодировка кириллицы (клиники, подписи на графиках)

Если в Metabase на осях или в таблице вместо русских букв отображаются знаки вопроса (`????`):

1. **Firebird:** в `egisz_corp.yaml` задайте корректный `firebird.charset` для вашей БД. Для классических русскоязычных баз Infoclinica часто подходит **`WIN1251`**. Если ключ `charset` в YAML **не указан**, загрузчик конфигурации (`config_loader.py`) по умолчанию подставляет **`WIN1251`** (для БД в UTF8 явно укажите `UTF8`). После смены charset выполните полный цикл ETL, чтобы перезаписать `dim_clinics.jname` и связанные витрины.
2. **PostgreSQL / ETL:** соединение ETL с витриной использует клиентскую кодировку **UTF8**; Job применения схемы и StatefulSet задают `LANG`/`LC_ALL` и при необходимости `POSTGRES_INITDB_ARGS` для нового PVC — см. `k8s/postgres/`.
3. **Контейнер Metabase:** образ задаёт `LANG`/`LC_ALL` в `C.UTF-8`, чтобы скрипты провижининга и JDBC не ломали UTF-8 при передаче нативных SQL из `metabase_dashboards/`.

## Объекты для дашбордов

После `egisz-corp apply-schema` и успешного `egisz-corp sync`:

| Объект | Назначение |
|--------|------------|
| `v_egisz_transactions_enriched` | Техническая витрина (snake_case): факты + СЭМД + клиника; **не переименовывать колонки здесь** — от этого зависят другие представления и ETL. |
| `v_egisz_transactions_enriched_ui` | То же содержимое, колонки с **русскими подписями** для Metabase (имена как в `dim_column_display_labels`). |
| `v_rpt_documents_no_response` | Очередь «документы без ответа» (технические имена колонок). |
| `v_rpt_documents_no_response_ui` | То же для отчётов с русскими именами колонок. |
| `dim_column_display_labels` | Справочник сопоставления `source_object` + `source_column` → `display_label_ru` (синхронизирован с колонками `*_ui`). |
| `fact_egisz_transactions` | Сырой факт (JSON ошибок, статусы, `local_uid_semd`) |
| `stg_parse_errors` | Строки без `relatesToMessage` / битый XML в **MSGTEXT** |
| `dim_clinics` | `jname`, `jinn`, `fir_oid`, `mo_uid` (ETL: `JPERSONS` + `EGISZ_LICENSES` по `JID`) |
| `dim_semd_types` | KIND → наименование |
| `etl_state` | Контроль курсора `last_log_id` (не используйте как бизнес-время) |

Карточки в `metabase_dashboards/` по умолчанию читают **`v_egisz_transactions_enriched_ui`** и **`v_rpt_documents_no_response_ui`**, чтобы заголовки таблиц и осей совпадали с подписями без ручного дублирования в каждом запросе.

## Пример вопроса (SQL)

```sql
SELECT "Статус", COUNT(*)::bigint AS "Количество"
FROM v_egisz_transactions_enriched_ui
WHERE "Обработано" > NOW() - INTERVAL '7 days'
GROUP BY 1;
```

## Деплой и обновление карточек (GitHub Actions / CI)

JSON из `metabase_dashboards/` попадают в Metabase **только** когда в поде выполняется **`/app/setup-dashboards.sh`** (его вызывает **`metabase/provision.sh`** при старте контейнера Metabase).

Чтобы после merge в `main` изменения дашбордов реально применились:

1. **Соберите и выкатите образ Metabase**, а не только приложение ETL: `docker build -f metabase/Dockerfile -t …` (в репозитории цель `egisz-corp-metabase`, см. `start.ps1` / `k8s/metabase.yaml`). В образ копируется каталог `metabase_dashboards/`.
2. **Перезапустите Deployment Metabase**, чтобы снова выполнился `entrypoint` → `provision.sh` → `setup-dashboards.sh` (пересоздание коллекций и карточек из JSON).
3. Убедитесь, что в момент старта пода в Postgres уже есть таблицы витрины (`apply-schema` / Job). Иначе раньше провижининг **один раз** пропускался и больше не повторялся — в `provision.sh` добавлено **ожидание схемы** (до ~10 минут) перед `setup-dashboards.sh`.

Локально: `docker build -f metabase/Dockerfile -t egisz-corp-metabase:latest .` и перезапуск контейнера Metabase.

### Почему после деплоя «ничего не изменилось»

1. **Открыт не тот объект в Metabase.** На **«Управленческий дашборд»** карточки **«% Ошибок за период»** и **«Всего обработано за период»** — это **таблицы** с разрезом (СЭМД, клиника; для доли ошибок ещё тип ответа ЕГИСЗ). Дополнительные графики по ошибкам — во втором ряду того же дашборда. На **«Тренды и динамика»** ось «Дата» строится по `Дата регистрации` из ответа ЕГИСЗ (fallback на `Обработано`), а не только по времени загрузки ETL.
2. **Не обновлён образ Metabase.** JSON из `metabase_dashboards/` копируется в образ при `docker build -f metabase/Dockerfile`. Деплой только ETL / другого сервиса **не** подтягивает новые SQL. Нужны пересборка `egisz-corp-metabase` и рестарт `deployment/metabase` (в Docker Desktop k8s скрипт `start.ps1` при deploy вызывает смену тега образа, чтобы под не оставался на старом `:latest`).
3. **Проверка:** `.\start.ps1 -Action verify` — скрипт в поде сверяет число `dashcards` на управленческом дашборде с числом карточек в `09_executive.json` внутри образа.
4. **Устаревший кэш образа на ноде:** в `k8s/metabase.yaml` для Metabase задано **`egisz-corp-metabase:local`** и **`imagePullPolicy: Never`**: в кластер попадает только то, что вы реально собрали (`docker build` + `docker tag … :local` — это делает `.\start.ps1 -Action build`). Старый вариант с одним **`:latest`** + `IfNotPresent` на ноде **не** обновлял digest, из‑за чего в поде оказывался образ **без** `/app/verify-corp-stack.sh`. После `build` и **`apply`/`deploy`** Docker Desktop дополнительно выставляет тег `latest-<время>` через `kubectl set image` — это принудительно обновляет pod. Если снова видите «no such file» для verify: `.\start.ps1 -Action build` и `.\start.ps1 -Action apply` (или полный `deploy`).

## Переменные Airflow (опционально)

- `egisz_corp_project_root` — корень репозитория с пакетом (для `sys.path`).
- `egisz_corp_config_path` — абсолютный путь к `egisz_corp.yaml`.

Расписание DAG: env `EGISZ_CORP_AIRFLOW_SCHEDULE` (cron или макрос Airflow, по умолчанию `@hourly`).
