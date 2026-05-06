"""Export Metabase dashboards to JSON (shape compatible with metabase/setup-dashboards.sh)."""

from __future__ import annotations

import io
import json
import os
import re
import sys
import urllib.error
import urllib.request
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

__all__ = [
    "build_export_zip_bytes",
    "build_static_bundled_dashboards_zip",
    "export_database_metadata_dump",
    "export_dashboards_zip",
    "main_cli",
]


def _using_metabase_api_key() -> bool:
    """If set (e.g. k8s Secret key `api_key`), use X-API-Key instead of /api/session (Metabase ≥ 0.49)."""
    return bool(os.environ.get("METABASE_API_KEY", "").strip())


def _api(mb: str, path: str, method: str = "GET", body: Any = None, token: str | None = None) -> Any:
    headers: dict[str, str] = {"Content-Type": "application/json"}
    session_login = path == "/api/session" and method == "POST" and body is not None
    if not session_login:
        api_key = os.environ.get("METABASE_API_KEY", "").strip()
        if api_key:
            headers["X-API-Key"] = api_key
        elif token:
            headers["X-Metabase-Session"] = token
    data = json.dumps(body).encode() if body is not None else None
    url = mb.rstrip("/") + path
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    with urllib.request.urlopen(req, timeout=120) as res:
        return json.loads(res.read().decode())


def _mb_normalize_list(raw: Any) -> list:
    if isinstance(raw, list):
        return raw
    if isinstance(raw, dict):
        if isinstance(raw.get("data"), list):
            return raw["data"]
        if isinstance(raw.get("items"), list):
            return raw["items"]
        inner = raw.get("data")
        if isinstance(inner, dict) and isinstance(inner.get("items"), list):
            return inner["items"]
    return []


def _get_field_info(mb: str, token: str, field_id: int) -> dict[str, str] | None:
    try:
        f = _api(mb, f"/api/field/{field_id}", "GET", None, token)
    except urllib.error.HTTPError:
        return None
    table = f.get("table") or {}
    return {"table": (table.get("name") or ""), "name": f.get("name")}


def extract_native_query_and_tags(dset: dict) -> tuple[str, dict] | None:
    if not dset or not isinstance(dset, dict):
        return None
    t = dset.get("type")
    if t == "native":
        nat = dset.get("native") or {}
        q = (nat.get("query") or "").strip()
        tags = nat.get("template-tags") or {}
        return (q, dict(tags)) if q else None
    if t == "query":
        qinner = dset.get("query") or {}
        if isinstance(qinner, dict) and qinner.get("type") == "native":
            nat = qinner.get("native") or {}
            if isinstance(nat, dict):
                q = (nat.get("query") or "").strip()
                tags = nat.get("template-tags") or {}
                if q:
                    return (q, dict(tags))
    if dset.get("lib/type") == "mbql/query":
        for stage in dset.get("stages") or []:
            if not isinstance(stage, dict):
                continue
            n = stage.get("native")
            if isinstance(n, str) and n.strip():
                return (n.strip(), dict(stage.get("template-tags") or {}))
    return None


def field_filters_from_tags(
    mb: str, token: str, template_tags: dict
) -> dict[str, dict[str, str]] | None:
    out: dict[str, dict[str, str]] = {}
    for tag_name, spec in (template_tags or {}).items():
        if not isinstance(spec, dict) or spec.get("type") != "dimension":
            continue
        dim = spec.get("dimension")
        if not (isinstance(dim, list) and len(dim) > 1 and dim[0] == "field"):
            continue
        fid = dim[1]
        if not isinstance(fid, int):
            continue
        info = _get_field_info(mb, token, fid)
        if not info or not info.get("table") or not info.get("name"):
            continue
        out[tag_name] = {"table_ref": info["table"], "field_name": info["name"]}
    return out or None


def card_to_item(mb: str, token: str, c: dict, db_placeholder: int = 1) -> dict:
    dset = c.get("dataset_query") or {}
    extracted = extract_native_query_and_tags(dset)
    if not extracted:
        query, tags = "", {}
    else:
        query, tags = extracted[0], dict(extracted[1])
    new_tags: dict = {}
    for k, v in tags.items():
        if not isinstance(v, dict):
            new_tags[k] = v
            continue
        new_tags[k] = {a: b for a, b in v.items() if a != "dimension"}
    mbf = field_filters_from_tags(mb, token, tags)
    item: dict[str, Any] = {
        "name": c.get("name", ""),
        "description": c.get("description", "") or "",
        "dataset_query": {
            "type": "native",
            "native": {"query": query, "template-tags": new_tags},
            "database": db_placeholder,
        },
        "display": c.get("display", "table"),
        "visualization_settings": c.get("visualization_settings") or {},
    }
    tid = c.get("table_id")
    if tid is not None:
        try:
            tinfo = _api(mb, f"/api/table/{tid}", "GET", None, token)
            item["table_ref"] = tinfo.get("name", "")
        except Exception:
            item["table_ref"] = ""
    if mbf:
        item["metabase-field-filters"] = mbf
    return item


def _sort_key(dc: dict) -> tuple:
    return (dc.get("row", 0), dc.get("col", 0), dc.get("id", 0))


def export_dashboard(mb: str, token: str, dashboard_id: int, db_placeholder: int = 1) -> dict:
    d = _api(mb, f"/api/dashboard/{dashboard_id}", "GET", None, token)
    out: dict[str, Any] = {
        "name": d.get("name", ""),
        "description": d.get("description", "") or "",
        "parameters": d.get("parameters") or [],
        "cards": [],
    }
    dcs = sorted(d.get("dashcards") or d.get("ordered_cards") or [], key=_sort_key)
    for dc in dcs:
        cid = dc.get("card_id")
        if not cid:
            continue
        c = _api(mb, f"/api/card/{cid}", "GET", None, token)
        dset = c.get("dataset_query") or {}
        ex = extract_native_query_and_tags(dset)
        if ex is None:
            continue
        item = card_to_item(mb, token, c, db_placeholder)
        item["sizeX"] = int(dc.get("size_x", dc.get("sizeX", 4)))
        item["sizeY"] = int(dc.get("size_y", dc.get("sizeY", 4)))
        item["row"] = int(dc.get("row", 0))
        item["col"] = int(dc.get("col", 0))
        out["cards"].append(item)
    return out


def _personal_scope_collection_ids(mb: str, token: str) -> set[int]:
    u = _api(mb, "/api/user/current", "GET", None, token)
    root = u.get("personal_collection_id")
    ids: set[int] = set()
    if root is not None and str(root) != "null":
        ids.add(int(root))
    cols_raw = _api(mb, "/api/collection", "GET", None, token)
    for c in _mb_normalize_list(cols_raw):
        if not isinstance(c, dict):
            continue
        if c.get("is_personal") and c.get("id") is not None:
            ids.add(int(c["id"]))
    return ids


def _iter_dashboards_in_scope(mb: str, token: str) -> list[tuple[int, str]]:
    allowed = _personal_scope_collection_ids(mb, token)
    seen: dict[int, str] = {}
    limit, offset = 200, 0
    first_page_id: str | None = None
    while True:
        page = _api(mb, f"/api/dashboard?limit={limit}&offset={offset}", "GET", None, token)
        arr = _mb_normalize_list(page)
        if not arr:
            break
        cur_first = str(arr[0].get("id", "")) if arr else ""
        if offset > 0 and first_page_id and cur_first == first_page_id:
            break
        if offset == 0:
            first_page_id = cur_first
        for d in arr:
            if not isinstance(d, dict):
                continue
            if d.get("archived"):
                continue
            cid = d.get("collection_id")
            if cid is None:
                continue
            if int(cid) not in allowed:
                continue
            did = d.get("id")
            name = (d.get("name") or "").strip()
            if did is not None:
                seen[int(did)] = name
        if len(arr) < limit:
            break
        offset += limit
        if offset > 20000:
            break
    return sorted(seen.items(), key=lambda x: x[0])


def export_database_metadata_dump(mb: str, token: str | None) -> dict[str, Any]:
    """Снимок метаданных БД Metabase: таблицы и поля (как setup-dashboards.sh для resolve_field_id).

    Для каждой неархивной, несэмпловой БД вызывается
    ``GET /api/database/:id/metadata?include_hidden=true``.
    """
    raw = _api(mb, "/api/database", "GET", None, token)
    items = _mb_normalize_list(raw)
    out: dict[str, Any] = {
        "exported_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "metabase_url": mb.rstrip("/"),
        "databases": [],
        "errors": [],
    }
    for db in items:
        if not isinstance(db, dict):
            continue
        if db.get("archived"):
            continue
        if db.get("is_sample"):
            continue
        did = db.get("id")
        if did is None:
            continue
        did_i = int(did)
        entry: dict[str, Any] = {
            "id": did_i,
            "name": db.get("name"),
            "engine": db.get("engine"),
        }
        try:
            meta = _api(mb, f"/api/database/{did_i}/metadata?include_hidden=true", "GET", None, token)
            entry["metadata"] = meta
        except Exception as e:
            out["errors"].append({"database_id": did_i, "error": str(e)})
            continue
        out["databases"].append(entry)
    return out


def _existing_dashboard_name_to_filename(out_dir: str) -> dict[str, str]:
    """Map exact Metabase dashboard title -> existing repo filename (for stable names like 01_operational.json)."""
    m: dict[str, str] = {}
    if not os.path.isdir(out_dir):
        return m
    for fn in os.listdir(out_dir):
        if not fn.endswith(".json"):
            continue
        path = os.path.join(out_dir, fn)
        try:
            with open(path, encoding="utf-8") as f:
                j = json.load(f)
            n = (j.get("name") or "").strip()
            if n:
                m[n] = fn
        except (OSError, json.JSONDecodeError, TypeError):
            continue
    return m


def _safe_zip_entry_name(dash_id: int, name: str) -> str:
    base = re.sub(r"[^\w.\-]+", "_", name, flags=re.UNICODE).strip("._") or "dashboard"
    base = base[:120]
    return f"{dash_id}_{base}.json"


def export_dashboards_zip(
    mb: str,
    user: str,
    password: str,
    *,
    db_placeholder: int = 1,
) -> tuple[bytes, str]:
    """Return (zip_bytes, suggested_filename)."""
    token: str | None = None
    if not _using_metabase_api_key():
        try:
            token = _api(mb, "/api/session", "POST", {"username": user, "password": password})["id"]
        except urllib.error.HTTPError as e:
            if e.code == 401:
                raise RuntimeError(
                    "Metabase: HTTP 401 при входе (/api/session). Проверьте email и password в Secret "
                    "`metabase-admin` (переменные METABASE_ADMIN_*). Либо создайте API Key в Metabase "
                    "(Admin → Settings → Authentication → API Keys) и добавьте в тот же Secret ключ `api_key` "
                    "— он передаётся как METABASE_API_KEY (заголовок X-API-Key), без пароля в запросах."
                ) from e
            raise
    try:
        pairs = _iter_dashboards_in_scope(mb, token)
    except urllib.error.HTTPError as e:
        if e.code == 401:
            raise RuntimeError(
                "Metabase: HTTP 401 при вызове API. Если пароль верный, задайте METABASE_API_KEY "
                "(ключ `api_key` в Secret `metabase-admin`, optional) — программный доступ через X-API-Key."
            ) from e
        raise
    if not pairs:
        raise RuntimeError("В Metabase не найдено дашбордов в личной коллекции администратора.")
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for did, title in pairs:
            try:
                data = export_dashboard(mb, token, did, db_placeholder)
            except urllib.error.HTTPError as e:
                if e.code == 401:
                    raise RuntimeError(
                        "Metabase: HTTP 401 при экспорте дашборда. Задайте METABASE_API_KEY "
                        "(API Keys в Metabase) или обновите пароль администратора в Secret."
                    ) from e
                raise
            raw = json.dumps(data, ensure_ascii=False, indent=2) + "\n"
            zf.writestr(_safe_zip_entry_name(did, title), raw.encode("utf-8"))
        try:
            meta_dump = export_database_metadata_dump(mb, token)
        except Exception as e:
            meta_dump = {
                "exported_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                "metabase_url": mb.rstrip("/"),
                "databases": [],
                "errors": [{"database_id": None, "error": str(e)}],
            }
        zf.writestr(
            "metabase_database_metadata.json",
            (json.dumps(meta_dump, ensure_ascii=False, indent=2) + "\n").encode("utf-8"),
        )
    ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    fname = f"egisz_metabase_dashboards_{ts}.zip"
    return buf.getvalue(), fname


def build_static_bundled_dashboards_zip() -> tuple[bytes, str]:
    """ZIP all *.json (+ optional field_filter_defaults.yaml) from bundled repo directory. No Metabase API."""
    src = _resolve_static_dashboards_dir()
    names = sorted(fn for fn in os.listdir(src) if fn.endswith(".json"))
    if not names:
        raise RuntimeError(f"В каталоге {src!r} нет файлов *.json.")
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for fn in names:
            path = os.path.join(src, fn)
            with open(path, "rb") as f:
                zf.writestr(fn, f.read())
        yaml_path = os.path.join(src, "field_filter_defaults.yaml")
        if os.path.isfile(yaml_path):
            with open(yaml_path, "rb") as f:
                zf.writestr("field_filter_defaults.yaml", f.read())
    ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    return buf.getvalue(), f"egisz_metabase_dashboards_bundled_{ts}.zip"


def _resolve_static_dashboards_dir() -> str:
    """Directory with shipped dashboard JSON (conf-ui image or dev checkout)."""
    raw = os.environ.get("METABASE_STATIC_DASHBOARDS_DIR", "").strip()
    candidates: list[str] = []
    if raw:
        candidates.append(raw)
    candidates.append("/app/metabase_dashboards")
    here = Path(__file__).resolve().parent
    candidates.append(str(here.parent / "metabase_dashboards"))
    for c in candidates:
        if not c or not os.path.isdir(c):
            continue
        if any(name.endswith(".json") for name in os.listdir(c)):
            return c
    raise RuntimeError(
        "Не найден каталог статических дашбордов (ожидались *.json). "
        "Проверьте METABASE_STATIC_DASHBOARDS_DIR или наличие metabase_dashboards/ рядом с пакетом."
    )


def build_export_zip_bytes() -> tuple[bytes, str, str]:
    """Try live Metabase export; on failure return bundled repo ZIP (same UI download).

    Returns (zip_bytes, filename, source) where source is 'live' or 'bundled'.
    """
    mb = os.environ.get("METABASE_URL", "http://127.0.0.1:3000").rstrip("/")
    user = os.environ.get("METABASE_ADMIN_EMAIL", "admin@egisz.local")
    password = os.environ.get("METABASE_ADMIN_PASSWORD", "egisz")
    last: Exception | None = None
    try:
        blob, fn = export_dashboards_zip(mb, user, password)
        return blob, fn, "live"
    except Exception as e:
        last = e
    try:
        blob, fn = build_static_bundled_dashboards_zip()
        return blob, fn, "bundled"
    except Exception as e2:
        tail = f" Резерв из образа: {e2}" if last else ""
        if last:
            raise RuntimeError(f"Выгрузка из Metabase не удалась ({last}).{tail}") from last
        raise RuntimeError(str(e2)) from e2


def main_cli() -> int:
    """CLI: write each dashboard as separate JSON under METABASE_EXPORT_DIR or repo/metabase_dashboards."""
    mb = os.environ.get("METABASE_URL", "http://127.0.0.1:3000").rstrip("/")
    user = os.environ.get("METABASE_ADMIN_EMAIL", "admin@egisz.local")
    password = os.environ.get("METABASE_ADMIN_PASSWORD", "egisz")
    explicit = os.environ.get("METABASE_EXPORT_DIR", "").strip()
    if explicit:
        out_dir = explicit
    else:
        here = os.path.dirname(os.path.abspath(__file__))
        repo_dashboards = os.path.join(os.path.dirname(here), "metabase_dashboards")
        if os.path.isdir(repo_dashboards):
            out_dir = repo_dashboards
        else:
            out_dir = "/app/metabase_dashboards"
    os.makedirs(out_dir, exist_ok=True)
    token: str | None = None
    try:
        if not _using_metabase_api_key():
            token = _api(mb, "/api/session", "POST", {"username": user, "password": password})["id"]
    except urllib.error.HTTPError as e:
        if e.code == 401:
            print(
                "Metabase HTTP 401 at /api/session. Set METABASE_API_KEY (Secret key `api_key`) "
                "or fix METABASE_ADMIN_EMAIL / METABASE_ADMIN_PASSWORD.",
                file=sys.stderr,
            )
        else:
            print(f"Cannot log in to Metabase at {mb}: {e}", file=sys.stderr)
        return 1
    except Exception as e:
        print(f"Cannot log in to Metabase at {mb}: {e}", file=sys.stderr)
        return 1
    try:
        pairs = _iter_dashboards_in_scope(mb, token)
    except urllib.error.HTTPError as e:
        if e.code == 401:
            print(
                "Metabase HTTP 401 listing dashboards. Set METABASE_API_KEY (Secret key `api_key`) "
                "or fix admin credentials.",
                file=sys.stderr,
            )
        else:
            print(f"Metabase API error: {e}", file=sys.stderr)
        return 1
    if not pairs:
        print("ERROR: no dashboards in admin personal scope", file=sys.stderr)
        return 1
    name_to_fn = _existing_dashboard_name_to_filename(out_dir)
    for did, title in pairs:
        fn = name_to_fn.get(title.strip()) or _safe_zip_entry_name(did, title)
        path = os.path.join(out_dir, fn)
        print(f"Exporting dashboard_id={did} -> {fn}")
        data = export_dashboard(mb, token, did)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
            f.write("\n")
        print(f"  OK {len(data['cards'])} cards -> {path}")
    try:
        dump = export_database_metadata_dump(mb, token)
    except Exception as e:
        dump = {
            "exported_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "metabase_url": mb.rstrip("/"),
            "databases": [],
            "errors": [{"database_id": None, "error": str(e)}],
        }
    meta_path = os.path.join(out_dir, "metabase_database_metadata.json")
    with open(meta_path, "w", encoding="utf-8") as f:
        json.dump(dump, f, ensure_ascii=False, indent=2)
        f.write("\n")
    print(f"  OK database field metadata -> {meta_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main_cli())
