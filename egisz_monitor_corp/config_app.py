"""Flask app: web page to view/edit Firebird + Postgres YAML config."""

from __future__ import annotations

import os
from pathlib import Path

from flask import Flask, render_template_string, request, url_for

from egisz_monitor_corp.config_loader import default_config_path, load_corp_config, save_corp_config
from egisz_monitor_corp.fb_client import fetch_all
from egisz_monitor_corp.pg_warehouse import test_pg_connection

PAGE = """
<!doctype html>
<html lang="ru">
<head>
  <meta charset="utf-8"/>
  <title>EGISZ Corp — подключения</title>
  <style>
    body { font-family: system-ui, sans-serif; max-width: 52rem; margin: 2rem auto; padding: 0 1rem; }
    label { display: block; margin-top: 0.75rem; font-weight: 600; }
    input { width: 100%; padding: 0.4rem; box-sizing: border-box; }
    fieldset { margin: 1rem 0; border: 1px solid #ccc; border-radius: 6px; }
    button { margin-top: 1rem; padding: 0.5rem 1rem; cursor: pointer; }
    .ok { color: #0a0; } .err { color: #a00; }
    .hint { color: #555; font-size: 0.9rem; margin-top: 0.25rem; }
    code { background: #f4f4f4; padding: 0.1rem 0.3rem; }
  </style>
</head>
<body>
  <h1>Конфигурация БД (EGISZ Monitor Corp)</h1>
  <p class="hint">Файл: <code>{{ path }}</code>. Переопределение: переменная окружения <code>EGISZ_CORP_CONFIG</code>
     или <code>CONFIG_WRITE_PATH</code> для записи.</p>
  <fieldset>
    <legend>Подключение Firebird (в т.ч. с Windows)</legend>
    <p class="hint">Поля ниже — это <strong>TCP к серверу Firebird</strong> (как в DBeaver / isql): хост и порт — где слушает <code>firebird.conf</code> (часто <code>3050</code>),
      <strong>database</strong> — имя alias или полный путь к <code>.fdb</code> <em>на стороне сервера Firebird</em>, не путь на вашем ПК.</p>
    <p class="hint">DSN для драйвера Python (<code>firebird-driver</code>): <code>{{ fb.host }}/{{ fb.port }}:{{ fb.database }}</code> (см. также документацию пакета).</p>
    <p class="hint"><strong>Проверить Firebird</strong> выполняется на машине/в поде, где запущен этот веб-интерфейс. Если UI в Kubernetes, до Firebird должен быть доступ <strong>из пода</strong> (сеть, firewall).
      С Windows вы можете отдельно проверить те же host/port/database в DBeaver — это подтвердит доступность с вашей сети.</p>
  </fieldset>
  {% if message %}<p class="{{ 'ok' if ok else 'err' }}">{{ message }}</p>{% endif %}
  <form method="post" action="{{ url_for('save') }}">
    <fieldset>
      <legend>Firebird</legend>
      <label>host <input name="fb_host" value="{{ fb.host }}" required/></label>
      <label>port <input name="fb_port" type="number" value="{{ fb.port }}" required/></label>
      <label>database (alias или путь на сервере) <input name="fb_database" value="{{ fb.database }}" required/></label>
      <label>user <input name="fb_user" value="{{ fb.user }}" required/></label>
      <label>password <input name="fb_password" type="password" value="{{ fb.password }}" autocomplete="current-password"/></label>
      <label>charset <input name="fb_charset" value="{{ fb.charset }}"/></label>
    </fieldset>
    <fieldset>
      <legend>PostgreSQL</legend>
      <p class="hint"><strong>PostgreSQL (Kubernetes):</strong> из пода витрины — <code>postgres.egisz-corp.svc.cluster.local:5432</code> (см. <code>k8s/README.md</code>). Port-forward с ПК: <code>kubectl -n egisz-corp port-forward svc/postgres 5432:5432</code>, тогда host <code>127.0.0.1</code>.</p>
      <p class="hint"><strong>Kubernetes:</strong> см. <code>k8s/postgres/</code> — из пода к сервису обычно <code>postgres.egisz-corp.svc.cluster.local:5432</code> (порт сервиса <code>5432</code>).
        С вашего ПК без VPN: <code>kubectl port-forward -n egisz-corp svc/postgres 5432:5432</code>, в форме host <code>127.0.0.1</code>, port <code>5432</code>.</p>
      <label>host <input name="pg_host" value="{{ pg.host }}" required/></label>
      <label>port <input name="pg_port" type="number" value="{{ pg.port }}" required/></label>
      <label>database <input name="pg_database" value="{{ pg.database }}" required/></label>
      <label>user <input name="pg_user" value="{{ pg.user }}" required/></label>
      <label>password <input name="pg_password" type="password" value="{{ pg.password }}" autocomplete="current-password"/></label>
      <label>schema <input name="pg_schema" value="{{ pg.schema }}"/></label>
    </fieldset>
    <fieldset>
      <legend>ETL (фрагмент)</legend>
      <label>batch_size <input name="etl_batch" type="number" value="{{ etl.batch_size }}"/></label>
      <label>sync_window_days <input name="etl_sync_days" type="number" value="{{ etl.sync_window_days }}"/></label>
      <label><input type="checkbox" name="etl_full_scan" value="1" {{ 'checked' if etl.full_scan else '' }}/> full_scan</label>
    </fieldset>
    <button type="submit">Сохранить в YAML</button>
  </form>
  <form method="post" action="{{ url_for('test_fb') }}" style="display:inline;margin-right:0.5rem;">
    <button type="submit">Проверить Firebird</button>
  </form>
  <form method="post" action="{{ url_for('test_pg') }}" style="display:inline;">
    <button type="submit">Проверить PostgreSQL</button>
  </form>
  <fieldset>
    <legend>Синхронизация Firebird -&gt; PostgreSQL</legend>
    <p class="hint">Запускает полный цикл ETL в фоне на сервере, где работает это приложение (под в k8s или процесс на ПК). Не закрывайте вкладку до завершения; статус обновляется ниже.</p>
    <button type="button" id="btnSync">Запустить синхронизацию</button>
    <pre id="syncStatus" style="background:#f8f8f8;padding:0.75rem;border-radius:6px;min-height:4rem;white-space:pre-wrap;font-size:0.9rem;"></pre>
  </fieldset>
  <script>
  async function pollSync() {
    const el = document.getElementById('syncStatus');
    try {
      const r = await fetch('/api/sync/status');
      const j = await r.json();
      const parts = [j.message || '', j.running ? 'Статус: выполняется' : 'Статус: ожидание'];
      if (j.error) parts.push('Ошибка: ' + j.error);
      if (j.last_stats) parts.push(JSON.stringify(j.last_stats, null, 2));
      el.textContent = parts.filter(Boolean).join(String.fromCharCode(10));
    } catch (e) { el.textContent = 'Ошибка опроса: ' + e; }
  }
  document.getElementById('btnSync').onclick = async function() {
    const el = document.getElementById('syncStatus');
    el.textContent = 'Запрос...';
    const r = await fetch('/api/sync/start', { method: 'POST' });
    const j = await r.json();
    el.textContent = j.message || JSON.stringify(j);
    pollSync();
  };
  setInterval(pollSync, 3000);
  pollSync();
  </script>
</body>
</html>
"""


def create_app() -> Flask:
    app = Flask(__name__)
    try:
        app.json.ensure_ascii = False  # type: ignore[attr-defined]
    except Exception:
        pass

    def config_path() -> Path:
        w = os.environ.get("CONFIG_WRITE_PATH")
        if w:
            return Path(w).expanduser().resolve()
        return default_config_path()

    @app.get("/")
    def index():  # type: ignore[no-untyped-def]
        p = config_path()
        if not p.is_file():
            return (
                f"<p>Нет файла конфигурации: <code>{p}</code>. Скопируйте "
                f"<code>config/egisz_corp.example.yaml</code> → <code>config/egisz_corp.yaml</code>.</p>",
                404,
            )
        cfg = load_corp_config(p)
        return render_template_string(
            PAGE,
            path=str(p),
            fb=cfg.firebird,
            pg=cfg.postgres,
            etl=cfg.etl,
            message=None,
            ok=True,
        )

    @app.post("/save")
    def save():  # type: ignore[no-untyped-def]
        p = config_path()
        old = {}
        if p.is_file():
            import yaml

            old = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
        if not isinstance(old, dict):
            old = {}
        old.setdefault("firebird", {})
        old.setdefault("postgres", {})
        old.setdefault("etl", {})
        old.setdefault("metabase", old.get("metabase") or {})

        old["firebird"].update(
            {
                "host": request.form.get("fb_host", "").strip(),
                "port": int(request.form.get("fb_port") or 3050),
                "database": request.form.get("fb_database", "").strip(),
                "user": request.form.get("fb_user", "").strip(),
                "password": request.form.get("fb_password", ""),
                "charset": request.form.get("fb_charset", "UTF8").strip() or "UTF8",
            }
        )
        old["postgres"].update(
            {
                "host": request.form.get("pg_host", "").strip(),
                "port": int(request.form.get("pg_port") or 5432),
                "database": request.form.get("pg_database", "").strip(),
                "user": request.form.get("pg_user", "").strip(),
                "password": request.form.get("pg_password", ""),
                "schema": request.form.get("pg_schema", "public").strip() or "public",
            }
        )
        old["etl"]["batch_size"] = int(request.form.get("etl_batch") or 500)
        old["etl"]["sync_window_days"] = int(request.form.get("etl_sync_days") or 30)
        old["etl"]["full_scan"] = bool(request.form.get("etl_full_scan"))

        save_corp_config(old, p)
        os.environ["EGISZ_CORP_CONFIG"] = str(p)
        cfg = load_corp_config(p)
        return render_template_string(
            PAGE,
            path=str(p),
            fb=cfg.firebird,
            pg=cfg.postgres,
            etl=cfg.etl,
            message="Сохранено.",
            ok=True,
        )

    @app.post("/test-fb")
    def test_fb():  # type: ignore[no-untyped-def]
        p = config_path()
        msg, ok = "Firebird: OK", True
        try:
            cfg = load_corp_config(p)
            fetch_all(cfg.firebird, "SELECT 1 AS OK FROM RDB$DATABASE")
        except Exception as e:  # pragma: no cover
            msg, ok = f"Firebird: {e}", False
        cfg = load_corp_config(p)
        return render_template_string(
            PAGE, path=str(p), fb=cfg.firebird, pg=cfg.postgres, etl=cfg.etl, message=msg, ok=ok
        )

    @app.post("/test-pg")
    def test_pg():  # type: ignore[no-untyped-def]
        p = config_path()
        msg, ok = "PostgreSQL: OK", True
        try:
            cfg = load_corp_config(p)
            test_pg_connection(cfg.postgres)
        except Exception as e:  # pragma: no cover
            msg, ok = f"PostgreSQL: {e}", False
        cfg = load_corp_config(p)
        return render_template_string(
            PAGE, path=str(p), fb=cfg.firebird, pg=cfg.postgres, etl=cfg.etl, message=msg, ok=ok
        )

    from egisz_monitor_corp.sync_routes import register_sync_routes

    register_sync_routes(app, config_path)

    return app


def run_dev() -> None:
    import os

    app = create_app()
    host = os.environ.get("FLASK_RUN_HOST", "127.0.0.1")
    port = int(os.environ.get("FLASK_RUN_PORT", "8765"))
    app.run(host=host, port=port, debug=os.environ.get("FLASK_DEBUG") == "1", threaded=True)
