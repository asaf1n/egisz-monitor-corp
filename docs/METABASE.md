# Metabase на корпоративном DWH

**Доступ с ПК (порт 3000):** в `k8s/metabase.yaml` объявлен Service **`metabase`** (`type: LoadBalancer`); на Docker Desktop он обычно публикует **[http://127.0.0.1:3000/](http://127.0.0.1:3000/)** — без `kubectl port-forward`. Если `LoadBalancer` в Pending (не Docker Desktop) — `kubectl -n egisz-monitor port-forward svc/metabase 3000:3000`. В Deployment **нет `hostPort`** (иначе на однонодовом кластере новый под Metabase часто зависает в **Pending** из‑за занятого порта 3000 на ноде). После `.\start.ps1 -Action apply` всегда выполняется **`rollout restart` Metabase и conf-ui** — холодный JVM, миграции app DB и readiness; это **2–6+ минут** нормально. БД приложения Metabase при `apply` **не** сбрасывается (DROP/CREATE только при `deploy` / `reset-deploy` / `reset-metabase`).

### Долгий старт или под в Pending после apply

| Симптом | Что проверить |
|--------|----------------|
| **Pending** долго | `kubectl -n egisz-monitor describe pod -l app.kubernetes.io/name=metabase` → Events (раньше часто был конфликт **hostPort:3000**; в манифесте убран). Память ноды: запрос пода **1Gi** — увеличьте RAM у Docker Desktop / kind worker. |
| **Running**, но деплой «Progressing» | Это ожидание **readiness** (`/api/health`): JVM Metabase + миграции Flyway к БД `metabase` в Postgres. Логи: `kubectl -n egisz-monitor logs deploy/metabase --tail=100`. |
| Каждый **apply** долго | `apply` всегда перезапускает Metabase и conf-ui. Только правки Flask без полного цикла: **`.\start.ps1 -Action restart-conf-ui`**. После смены JSON дашбордов нужен **`build`** и rollout Metabase. |


**Где искать дашборды:** провижининг кладёт дашборды **в корень личной коллекции** пользователя Metabase, под которым идёт API-сессия (обычно первый администратор): `GET /api/user/current` → `personal_collection_id`. В UI это тот же пункт **«Персональная коллекция …»** / **Your personal collection** — откройте его: дашборды должны быть **на этой странице**, без отдельной вложенной папки. Проверка после деплоя: `.\start.ps1 -Action verify` или логи пода `metabase` (`provision.sh`, `verify-corp-stack.sh`).

### Первичный набор отчётов: только `metabase_dashboards/*.json`

**Источник отчётов** — только JSON в репозитории: один файл = один дашборд, native-вопросы **вложены** в JSON и создаются `setup-dashboards.sh` при провижининге, не через «Сохранить вопрос» в UI. Список: [`metabase_dashboards/README.md`](../metabase_dashboards/README.md).

- **Пустой Metabase:** после `POST /api/setup` (первый админ) и появления схемы витрины в Postgres `metabase/provision.sh` вызывает `setup-dashboards.sh`, который **с нуля** создаёт весь набор. Повторный прогон **идемпотентен** для одноимённых дашбордов/карточек в той же персональной коллекции: скрипт удаляет устаревшие сущности по списку из `*.json` и снова импортирует файлы.
- **Изменение SQL или состава карт:** правьте JSON в `metabase_dashboards/`, **пересоберите** образ `egisz-monitor-metabase` (в каталог копируется весь `metabase_dashboards/`, см. `metabase/Dockerfile`) и **перезапустите** deployment Metabase — `entrypoint` снова выполнит `provision.sh`. Без обновлённого образа под **не** увидит новые JSON.
- **Только API на localhost, без k8s:** `.\metabase\provision-local.ps1` — собирает образ и запускает `setup-dashboards.sh` с **монтированием** текущего `metabase_dashboards` с диска, так что дашборды в Metabase соответствуют рабочей копии репозитория, даже до коммита в образ.

**Учётная запись администратора Metabase:** `admin@egisz.local` / `egisz`. В Kubernetes значения приходят из Secret `metabase-admin` (`METABASE_ADMIN_EMAIL`, `METABASE_ADMIN_PASSWORD` в `k8s/metabase.yaml`); шаблон — `k8s/metabase-admin-secret.example.yaml`. Локальный `.\start.ps1` при записи секретов использует тот же набор. Для `.\metabase\provision-local.ps1` задайте те же переменные или передайте `-AdminEmail` / `-AdminPassword`.

### Главная Metabase: «Приветствую …» и «Произошла ошибка»

Текст на **домашней** странице (`/`) идёт из Metabase; он **не** из этого репозитория. Блок с ошибкой значит, что один из запросов виджета главной (активность, закреплённое, «продолжить») вернул сбой — витрина и дашборды в коллекции при этом часто **работают**.

Что сделать по шагам:

1. **Открыть дашборды минуя главную:** сайдбар → **«Персональная коллекция …»** → дашборд (например **«Управленческий дашборд»**). Либо вручную URL вида `/collection/<personal_collection_id>` (id смотрите в выводе verify или в логах провижининга).
2. **Site URL:** **Admin (шестерёнка) → Administration → Settings → General → Site URL** — должен совпадать с тем, как вы реально открываете Metabase (одинаковый хост и порт: `http://127.0.0.1:3000` и `http://localhost:3000` для браузера — это **разные** origin). После смены перезагрузите страницу. В k8s при постоянном способе доступа можно задать переменную **`MB_SITE_URL`** в Deployment (см. комментарий в `k8s/metabase.yaml`).
3. **После пересоздания карточек провижингом** старые **закладки** или закрепление на главной могли указывать на несуществующие id: снимите закладки с «битых» вопросов/дашбордов или откройте Metabase в **режиме инкогнито** и зайдите снова.
4. **Диагностика:** в браузере **DevTools → Network** обновите `/` и найдите запрос к `/api/...` со статусом 4xx/5xx; на сервере: `kubectl -n egisz-monitor logs deploy/metabase --tail=200`.

## Подключение

1. В Metabase: **Admin → Databases → Add database → PostgreSQL**.
2. Используйте те же параметры, что в `config/egisz_monitor.yaml` → секция `postgres`.
3. Схема по умолчанию: `public` (или значение `postgres.schema` в YAML).

## Кодировка кириллицы (клиники, подписи на графиках)

Если в Metabase на осях или в таблице вместо русских букв отображаются знаки вопроса (`????`):

1. **Firebird:** в `egisz_monitor.yaml` задайте корректный `firebird.charset` для вашей БД. Для классических русскоязычных баз Infoclinica часто подходит **`WIN1251`**. Если ключ `charset` в YAML **не указан**, загрузчик конфигурации (`config_loader.py`) по умолчанию подставляет **`WIN1251`** (для БД в UTF8 явно укажите `UTF8`). После смены charset выполните полный цикл ETL, чтобы перезаписать `dim_clinics.jname` и связанные витрины.
2. **PostgreSQL / ETL:** соединение ETL с витриной использует клиентскую кодировку **UTF8**; Job применения схемы и StatefulSet задают `LANG`/`LC_ALL` и при необходимости `POSTGRES_INITDB_ARGS` для нового PVC — см. `k8s/postgres/`.
3. **Контейнер Metabase:** образ задаёт `LANG`/`LC_ALL` в `C.UTF-8`, чтобы скрипты провижининга и JDBC не ломали UTF-8 при передаче нативных SQL из `metabase_dashboards/`.

## Объекты для дашбордов

После `egisz-monitor apply-schema` и успешного прогона ETL (CLI **`egisz-monitor sync`** или кнопка синхронизации в **Config UI** — см. ниже):

| Объект | Назначение |
|--------|------------|
| `v_egisz_transactions_enriched` | Техническая витрина (snake_case): факты + СЭМД + клиника; **не переименовывать колонки здесь** — от этого зависят другие представления и ETL. |
| `v_egisz_transactions_enriched_ui` | То же содержимое, колонки с **русскими подписями** для Metabase (имена как в `dim_column_display_labels`). В т.ч. **«Сводка ошибок»** — плоская строка по `errors_json` (функция `egisz_friendly_errors_row`), без замены сырого JSON; **«Ошибки JSON»** — как в ответе РЭМД. |
| `public.egisz_friendly_error_item`, `public.egisz_friendly_errors_row` | SQL-функции в витрине: подсказка по одному элементу `code`/`message` и агрегат по всему массиву (используются в нативных вопросах и в колонке **«Сводка ошибок»**). |
| `v_rpt_documents_no_response` | Очередь «документы без ответа» (технические имена колонок). |
| `v_rpt_documents_no_response_ui` | То же для отчётов с русскими именами колонок. |
| `dim_column_display_labels` | Справочник сопоставления `source_object` + `source_column` → `display_label_ru` (синхронизирован с колонками `*_ui`). |
| `fact_egisz_transactions` | Сырой факт (JSON ошибок, статусы, `local_uid_semd`) |
| `stg_parse_errors` | Строки без `relatesToMessage` / битый XML в **MSGTEXT** |
| `dim_clinics` | `jname`, `jinn`, `fir_oid`, `mo_uid` (ETL: `JPERSONS` + `EGISZ_LICENSES` по `JID`) |
| `dim_semd_types` | KIND → наименование |
| `etl_state` | Контроль курсора `last_log_id` (не используйте как бизнес-время) |

Карточки в `metabase_dashboards/` по умолчанию читают **`v_egisz_transactions_enriched_ui`** и **`v_rpt_documents_no_response_ui`**, чтобы заголовки таблиц и осей совпадали с подписями без ручного дублирования в каждом запросе.

## Сбор данных в витрину и использование Metabase

### Синхронизация Firebird → PostgreSQL

Факты в витрину попадают через **`run_sync`** (`egisz_monitor_corp.etl`): только **SELECT** из Firebird (**firebird-driver**, на воркере нужен **fbclient** / **`FB_CLIENT_LIBRARY`**) и запись в PostgreSQL (**psycopg2**).

- **Курсор:** в таблице **`etl_state`** хранятся **`last_log_id`** (журнал **`EXCHANGELOG`**) и **`last_egmid`** (**`EGISZ_MESSAGES`**) по имени пайплайна из YAML (по умолчанию `firebird_exchangelog`). Режим **`full_scan: true`** сбрасывает оба курсора в 0 и перечитывает данные с начала (осторожно на больших базах).
- **Порядок в одном прогоне:** полный SELECT **`EGISZ_LICENSES`** + **`LEFT JOIN JPERSONS`** → **`stg_egisz_licenses_import`** → merge в **`dim_clinics`** → кэш ETL из PostgreSQL; постраничная выгрузка **`EGISZ_MESSAGES`** по **`EGMID`** выше курсора (пик **`source_max_egmid`** в **`etl_state`** при необходимости); **`EXCHANGELOG`** по **`LOGID`**, парсинг, **`stg_parse_errors`**, UPSERT, обновление **`last_log_id`**; исходящие → **`stg_egisz_outbound_documents`** по **`EGMID`** выше курсора на начало прогона. COUNT в Firebird для прогресса не выполняется.

**Как запустить тот же ETL:**

| Способ | Примечание |
|--------|------------|
| **`egisz-monitor sync`** | CLI из установленного пакета; путь к YAML — **`EGISZ_MONITOR_CONFIG`** или **`--config`**. |
| **Config UI** (кнопка синхронизации) | Тот же **`run_sync`**, что и в CLI, выполняется **в процессе Flask** (`sync_routes`, single-flight: повторный старт, пока идёт синк, отклоняется). Это **не** вызов внешнего бинарника из пода. |
| **Apache Airflow** | DAG **`egisz_monitor_firebird_to_postgres`** (`airflow/dags/egisz_monitor_etl_dag.py`): задача **`test_connections`**, затем **`monitor_sync`**; путь к конфигу — переменные **`egisz_monitor_project_root`** / **`egisz_monitor_config_path`** (см. раздел «Переменные Airflow» ниже). |
| **`start.ps1`** | При **`deploy`** / **`apply`** поднимается стек и схема витрины, но **полный прогон ETL не вшит** в скрипт по умолчанию; после деплоя данные загружают из **Config UI**, **`kubectl … exec deploy/conf-ui -- egisz-monitor sync`** (см. вывод `start.ps1` после деплоя) или **Airflow**. |

Подробнее по стеку и шагам см. **`README.md`** (разделы «Стек технологий», «Синхронизация», «Выборка данных») и **`AGENTS.md`**. Диагностика объёмов: **`docs/SYNC_DIAGNOSTICS.md`** (часть формулировок про окно по датам может не совпадать с текущим SQL ETL — ориентир **`egisz_monitor_corp/sql_util.py`**).

Порядок, в котором появляются **факты** и обновляются **дашборды** (колонка **«Сводка ошибок»** и карточки, вызывающие `egisz_friendly_*`, требуют актуального `001_schema.sql` в Postgres).

1. **Схема витрины в PostgreSQL** — `egisz_reports` должна содержать таблицы/представления и функции (`egisz-monitor apply-schema`, k8s Job `egisz-reports-schema-init`, либо `.\start.ps1` при **`deploy` / `apply` / `start`** — порядок файлов в `sql/schema_apply_order.txt`). Без шага (1) запросы с **«Сводка ошибок»** или `egisz_friendly_error_item` вернут ошибку.
2. **ETL (загрузка фактов)** — см. подраздел **«Синхронизация Firebird → PostgreSQL»** выше: после появления схемы каждый успешный прогон **догружает** новые callback-и по курсору **`LOGID`**; колонка **«Сводка ошибок»** в UI витрины пересчитывается при **чтении** (SQL), менять ETL ради неё не нужно.
3. **Metabase** смотрит на ту же БД `egisz_reports` и только **читает** витрину. После смены JSON-дашбордов или `Dockerfile` Metabase: **`docker build -f metabase/Dockerfile`**, бамп тега **`egisz-monitor-metabase:k8s-v15`** (см. `k8s/metabase.yaml`), `kubectl rollout restart deployment/metabase` или `.\start.ps1 -Action apply` (подхватит образ, провижининг при старте; если в БД metabase уже есть 11 EGISZ-дашбордов с именами «01 …» — провижининг **пропускается**, dashboard ID не меняются). Если нужно принудительно пересобрать дашборды (изменили JSON и хотите переналить): `.\start.ps1 -Action reset-metabase` (DROP/CREATE БД metabase в Postgres) или временно установить env `METABASE_FORCE_PROVISION=true` в `k8s/metabase.yaml`. Локальный kind: после `build` — `kind load` образов, как в `start.ps1`. При **только** обновлении **витрины** без смены образа Metabase достаточно повторного **apply-schema** и/или ETL; пересобирать образ Metabase **не** обязательно.

**Разделители в «Сводка ошибок»:** между элементами массива `errors_json` в одной транзакции — **·** (средняя точка); внутри многочастевого Schematron в одном `message` — **—** (короткое тире в SQL). Понятные бизнес-сообщения ГИП (`не соответствует данным ГИП` и т.д.) **не** перезаписываются.

## Пример вопроса (SQL)

```sql
SELECT "Статус", COUNT(*)::bigint AS "Количество"
FROM v_egisz_transactions_enriched_ui
WHERE "Обработано" > NOW() - INTERVAL '7 days'
GROUP BY 1;
```

## Metabase 0.60+ и фильтры дат на дашборде

Во native-вопросах важно не путать **базовую (basic) date-переменную** и **field filter** (тип `dimension`):
[SQL parameters: Field filters](https://www.metabase.com/docs/v0.60/questions/native-editor/field-filters) / [variables](https://www.metabase.com/docs/v0.60/questions/native-editor/sql-parameters.html) — *«If you add a basic Date variable… it’s only possible to use the dashboard filter option Single Date. So if you’re trying to use one of the other Time options on the dashboard, you’ll need to change the variable to a field filter and map it to a date field.»*

В провижининге из `metabase_dashboards/*.json`:

- в SQL: `WHERE 1=1 [[AND {{dwh_date}}]]` (у одного и того же **имени** тега `dwh_date` на разных карточках может быть разный физический столбец, см. ниже);
- в `template-tags` для `dwh_date` указаны `type: "dimension"` и `widget-type: "date/all-options"`;
- на **карточке** рядом с `dataset_query` — объект **`metabase-field-filters`**: скрипт `metabase/setup-dashboards.sh` подставит `dimension: ["field", <field_id>, null]` в API Metabase, сопоставив `table_ref` + `field_name` с полями после `sync` БД. Имя поля можно взять из `dim_column_display_labels` (русские подписи в `*_ui`) или, при необходимости, совпадение с **display name** (в `setup-dashboards.sh` — fallback по `display_name` в метаданных).

**Один** параметр дашборда (slug `dwh_date_filter`) визуально один виджет; внутри вопросов `dwh_date` может быть сопоставлен, например, с «**Обработано**» (витрина) или с «**Отправлено**» (очередь) — важно, что **каждое** сохранённое применяет к своему `field` диапазон, заданный пользователем. До замены **«N дней от MAX(даты) в витрине»** окно задаётся **календарём/относительным диапазоном** (последние 7/30 суток и т.д.) в UI Metabase.

**Требуется** после `POST /api/database/:id/sync_schema` в скрипте, чтобы `resolve_field_id` нашёл `field_name`. Если сопоставление не сработает, в логе provижнинга будет: `metabase-field-filters: field not found`.

## Деплой и обновление карточек (GitHub Actions / CI)

JSON из `metabase_dashboards/` попадают в Metabase **только** когда в поде выполняется **`/app/setup-dashboards.sh`** (его вызывает **`metabase/provision.sh`** при старте контейнера Metabase).

Чтобы после merge в `main` изменения дашбордов реально применились:

1. **Соберите и выкатите образ Metabase**, а не только приложение ETL: `docker build -f metabase/Dockerfile -t …` (в репозитории цель `egisz-monitor-metabase`, см. `start.ps1` / `k8s/metabase.yaml`). В образ копируется каталог `metabase_dashboards/`.
2. **Перезапустите Deployment Metabase**, чтобы снова выполнился `entrypoint` → `provision.sh` → `setup-dashboards.sh` (пересоздание коллекций и карточек из JSON).
3. Убедитесь, что в момент старта пода в Postgres уже есть таблицы витрины (`apply-schema` / Job). Иначе раньше провижининг **один раз** пропускался и больше не повторялся — в `provision.sh` добавлено **ожидание схемы** (до ~10 минут) перед `setup-dashboards.sh`.

Локально: `docker build -f metabase/Dockerfile -t egisz-monitor-metabase:latest .` и перезапуск контейнера Metabase.

### Почему после деплоя «ничего не изменилось»

1. **Открыт не тот объект в Metabase.** На **«Управленческий дашборд»** карточки **«09 · Доля ошибок по срезам (таблица)»** и **«09 · Объём по СЭМД и клиникам»** — это **таблицы** с разрезом (СЭМД, клиника; для доли ошибок ещё тип ответа ЕГИСЗ). Дополнительные графики по ошибкам — во втором ряду того же дашборда. На **«Тренды и динамика»** ось «Дата» строится по `Дата регистрации` из ответа ЕГИСЗ (fallback на `Обработано`), а не только по времени загрузки ETL.
2. **Не обновлён образ Metabase.** JSON из `metabase_dashboards/` копируется в образ при `docker build -f metabase/Dockerfile`. Деплой только ETL / другого сервиса **не** подтягивает новые SQL. Нужны пересборка `egisz-monitor-metabase` и рестарт `deployment/metabase` (в Docker Desktop k8s скрипт `start.ps1` при deploy вызывает смену тега образа, чтобы под не оставался на старом `:latest`).
3. **Проверка:** `.\start.ps1 -Action verify` — скрипт в поде сверяет число `dashcards` на управленческом дашборде с числом карточек в `09_executive.json` внутри образа.
4. **Устаревший кэш на ноде:** в `k8s/metabase.yaml` задан версионируемый тег **`egisz-monitor-metabase:k8s-v15`** (не `latest`): при смене JSON/скриптов **увеличьте** тег в манифесте и в `start.ps1` (`docker tag … :k8s-v…`), затем `kubectl apply -f k8s/metabase.yaml` и **`rollout restart`** — иначе Docker Desktop может оставить старый digest. Дополнительно `.\start.ps1 -Action build` создаёт `:local` для `provision-local.ps1`. Если «no such file» к verify: `build` + `apply`, либо `.\metabase\force-k8s-mb-image.ps1`.

## Переменные Airflow (опционально)

- `egisz_monitor_project_root` — корень репозитория с пакетом (для `sys.path`).
- `egisz_monitor_config_path` — абсолютный путь к `egisz_monitor.yaml`.

Расписание DAG: env `EGISZ_MONITOR_AIRFLOW_SCHEDULE` (cron или макрос Airflow, по умолчанию `@hourly`).
