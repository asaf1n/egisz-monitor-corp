from __future__ import annotations

import io
import json
import os
import zipfile
from datetime import datetime, timezone
from typing import Any, Mapping, Tuple

from egisz_monitor_corp.metabase_export import build_export_zip_bytes

__all__ = ["build_metabase_settings_bundle_zip_bytes"]


def _get_form_str(form: Mapping[str, Any], key: str) -> str:
    v = form.get(key)
    if v is None:
        return ""
    return str(v).strip()


def build_metabase_settings_bundle_zip_bytes(form: Mapping[str, Any]) -> Tuple[bytes, str, str]:
    """ZIP для выгрузки настроек Metabase: дашборды, карточки/вопросы, field filters, bootstrap.

    Contents:
    - metabase_dashboards/*.json — экспорт дашбордов (живой API или эталон из образа)
    - metabase_dashboards/field_filter_defaults.yaml — при наличии в эталонном ZIP
    - metabase_database_metadata.json — дамп таблиц/полей из Metabase (только при живой выгрузке)
    - metabase_bootstrap.json — параметры Postgres и базовые настройки сайта для provision
    - metabase_import_howto.txt — краткая инструкция по импорту

    Returns (zip_bytes, suggested_filename, dashboards_source) where dashboards_source is 'live' or 'bundled'.
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
            "why": "Параметры для настройки Metabase (Admin + DWH) и импорта дашбордов/карточек EGISZ.",
            "dashboards": "JSON дашбордов включает сохранённые вопросы (карточки), параметры дашборда и сопоставления field-filter'ов для dimension template-tags.",
            "fields": "metabase_database_metadata.json — снимок /api/database/:id/metadata (таблицы и поля DWH), см. metabase/setup-dashboards.sh.",
        },
    }

    howto = (
        "EGISZ Monitor Corp — выгрузка настроек Metabase (JSON)\n"
        "\n"
        "В архиве:\n"
        "- metabase_dashboards/*.json  — дашборды, вложенные сохранённые вопросы (карточки), параметры и фильтры\n"
        "- metabase_dashboards/field_filter_defaults.yaml — если есть (эталон из репозитория)\n"
        "- metabase_database_metadata.json — дамп полей/таблиц Metabase (только при выгрузке с живого инстанса)\n"
        "- metabase_bootstrap.json    — параметры подключения Postgres и базовые настройки сайта\n"
        "\n"
        "Импорт тем же механизмом, что в k8s-образе:\n"
        "- bootstrap (admin + Postgres): metabase/provision.sh\n"
        "- wipe+import JSON: metabase/setup-dashboards.sh\n"
        "\n"
        "Если API-экспорт недоступен, JSON дашбордов берётся из эталона metabase_dashboards/ в образе (source=bundled); дампа полей в архиве не будет.\n"
    )

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        with zipfile.ZipFile(io.BytesIO(dashboards_zip_bytes), "r") as src:
            for info in src.infolist():
                if info.is_dir():
                    continue
                name = info.filename.replace("\\", "/")
                base = name.split("/")[-1]
                lower = base.lower()
                if lower == "metabase_database_metadata.json":
                    zf.writestr("metabase_database_metadata.json", src.read(info.filename))
                elif lower.endswith(".json") or lower == "field_filter_defaults.yaml":
                    zf.writestr(f"metabase_dashboards/{base}", src.read(info.filename))

        zf.writestr("metabase_bootstrap.json", (json.dumps(bootstrap, ensure_ascii=False, indent=2) + "\n").encode("utf-8"))
        zf.writestr("metabase_import_howto.txt", howto.encode("utf-8"))

    ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    return buf.getvalue(), f"egisz_metabase_json_bundle_{ts}.zip", dashboards_source
