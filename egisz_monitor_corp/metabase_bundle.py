from __future__ import annotations

import io
import json
import os
import zipfile
from datetime import datetime, timezone
from typing import Any, Mapping, Tuple

from egisz_monitor_corp.metabase_export import build_export_zip_bytes

__all__ = ["build_empty_metabase_bundle_zip_bytes"]


def _get_form_str(form: Mapping[str, Any], key: str) -> str:
    v = form.get(key)
    if v is None:
        return ""
    return str(v).strip()


def build_empty_metabase_bundle_zip_bytes(form: Mapping[str, Any]) -> Tuple[bytes, str]:
    """Build a single ZIP meant to bootstrap an empty Metabase instance.

    Contents:
    - metabase_dashboards/*.json : exported dashboards bundle (live or bundled fallback)
    - metabase_bootstrap.json    : settings needed to recreate DB + site prefs
    - metabase_import_howto.txt  : minimal instructions for operators
    """
    dashboards_zip_bytes, dashboards_zip_name, dashboards_source = build_export_zip_bytes()

    # PostgreSQL settings are already in the form (same as /test-pg and /api/pg/backup).
    pg = {
        "host": _get_form_str(form, "pg_host") or "postgres",
        "port": int(_get_form_str(form, "pg_port") or "5432"),
        "database": _get_form_str(form, "pg_database") or "egisz_reports",
        "user": _get_form_str(form, "pg_user") or "egisz",
        "password": _get_form_str(form, "pg_password") or "egisz",
        "ssl": False,
    }

    # Metabase "site url" is not always part of YAML; keep optional.
    metabase = {
        "site_url": _get_form_str(form, "metabase_site_url") or (os.environ.get("EGISZ_METABASE_SITE_URL") or "").strip(),
        "site_name": _get_form_str(form, "metabase_site_name") or "EGISZ Monitor Corp",
        "site_locale": _get_form_str(form, "metabase_site_locale") or "ru",
        "dashboards_source": dashboards_source,
        "dashboards_zip_name": dashboards_zip_name,
    }

    bootstrap = {
        "metabase": metabase,
        "postgres_app_db": pg,
        "notes": {
            "why": "Этот файл — параметры для первичной настройки Metabase (Admin + подключение к DWH) и импорта EGISZ-дашбордов.",
            "dashboards": "JSON включает сохранённые вопросы (карточки), параметры дашбордов и сопоставления field-filter'ов для dimension template-tags.",
        },
    }

    howto = (
        "EGISZ Monitor Corp — пакет для пустого Metabase\n"
        "\n"
        "Внутри архива:\n"
        "- metabase_dashboards/*.json  — дашборды + сохранённые вопросы (карточки) + параметры/фильтры\n"
        "- metabase_bootstrap.json    — параметры подключения Postgres и базовые настройки сайта\n"
        "\n"
        "Импорт в пустой Metabase делается тем же механизмом, что и в k8s-образе:\n"
        "- bootstrap (создание admin + подключение Postgres): metabase/provision.sh\n"
        "- wipe+import JSON (коллекции/дашборды/карточки/связи фильтров): metabase/setup-dashboards.sh\n"
        "\n"
        "Подсказка: если API-экспорт недоступен, в пакете будет эталонный набор из metabase_dashboards/ (source=bundled).\n"
    )

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        # Copy dashboards JSON into a stable subfolder.
        with zipfile.ZipFile(io.BytesIO(dashboards_zip_bytes), "r") as src:
            for info in src.infolist():
                if info.is_dir():
                    continue
                name = info.filename.replace("\\", "/")
                if not name.lower().endswith(".json"):
                    continue
                zf.writestr(f"metabase_dashboards/{name.split('/')[-1]}", src.read(info.filename))

        zf.writestr("metabase_bootstrap.json", (json.dumps(bootstrap, ensure_ascii=False, indent=2) + "\n").encode("utf-8"))
        zf.writestr("metabase_import_howto.txt", howto.encode("utf-8"))

    ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    return buf.getvalue(), f"egisz_metabase_empty_bundle_{ts}.zip"

