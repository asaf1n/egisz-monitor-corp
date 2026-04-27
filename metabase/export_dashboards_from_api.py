#!/usr/bin/env python3
"""
Export dashboards from a running Metabase into setup-dashboards.sh JSON shape
(metabase_dashboards/01_operational.json, 09_executive.json).

Requires: port-forward to http://127.0.0.1:3000, admin user (admin@egisz.local / egisz).
Run from repo root:  py -3 metabase/export_dashboards_from_api.py

Do not use this to overwrite 01/09 with "source of truth" for field filter (dwh_date) —
a live instance may still be on op_period/period; the repo JSON is edited for dwh_date and
re-imported on provision. Use export for backup or after UI was aligned with repo.
"""
from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request
from typing import Any

MB = os.environ.get("METABASE_URL", "http://127.0.0.1:3000").rstrip("/")
USER = os.environ.get("METABASE_ADMIN_EMAIL", "admin@egisz.local")
PASS = os.environ.get("METABASE_ADMIN_PASSWORD", "egisz")
DB_PLACEHOLDER = 1


def api(path: str, method: str = "GET", body: Any = None, token: str | None = None) -> Any:
    headers = {"Content-Type": "application/json"}
    if token:
        headers["X-Metabase-Session"] = token
    data = json.dumps(body).encode() if body is not None else None
    r = urllib.request.Request(MB + path, data=data, headers=headers, method=method)
    with urllib.request.urlopen(r) as res:
        return json.loads(res.read().decode())


def get_field_info(token: str, field_id: int) -> dict[str, str] | None:
    try:
        f = api(f"/api/field/{field_id}", "GET", None, token)
    except urllib.error.HTTPError:
        return None
    table = f.get("table") or {}
    return {"table": (table.get("name") or ""), "name": f.get("name")}


def extract_native_query_and_tags(dset: dict) -> tuple[str, dict] | None:
    """Metabase 0.49+ classic native, 0.55+ `type: query` + nested native, 0.60+ `mbql/query` stages."""
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
    token: str, template_tags: dict
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
        info = get_field_info(token, fid)
        if not info or not info.get("table") or not info.get("name"):
            continue
        out[tag_name] = {
            "table_ref": info["table"],
            "field_name": info["name"],
        }
    return out or None


def card_to_item(token: str, c: dict) -> dict:
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
    mbf = field_filters_from_tags(token, tags)
    item: dict[str, Any] = {
        "name": c.get("name", ""),
        "description": c.get("description", "") or "",
        "dataset_query": {
            "type": "native",
            "native": {
                "query": query,
                "template-tags": new_tags,
            },
            "database": DB_PLACEHOLDER,
        },
        "display": c.get("display", "table"),
        "visualization_settings": c.get("visualization_settings") or {},
    }
    tid = c.get("table_id")
    if tid is not None:
        try:
            tinfo = api(f"/api/table/{tid}", "GET", None, token)
            item["table_ref"] = tinfo.get("name", "")
        except Exception:
            item["table_ref"] = ""
    if mbf:
        item["metabase-field-filters"] = mbf
    return item


def sort_key(dc: dict) -> tuple:
    return (dc.get("row", 0), dc.get("col", 0), dc.get("id", 0))


def export_dashboard(token: str, dashboard_id: int) -> dict:
    d = api(f"/api/dashboard/{dashboard_id}", "GET", None, token)
    out = {
        "name": d.get("name", ""),
        "description": d.get("description", "") or "",
        "parameters": d.get("parameters") or [],
        "cards": [],
    }
    dcs = sorted(d.get("dashcards") or [], key=sort_key)
    for dc in dcs:
        cid = dc.get("card_id")
        if not cid:
            continue
        c = api(f"/api/card/{cid}", "GET", None, token)
        dset = c.get("dataset_query") or {}
        ex = extract_native_query_and_tags(dset)
        if ex is None:
            if c.get("query_type") == "native":
                print(
                    f"  ERROR: card id={cid} name={c.get('name')!r} query_type=native but no extractable SQL; skip",
                    file=sys.stderr,
                )
            else:
                print(
                    f"  WARN: skip non-native card id={cid} name={c.get('name')!r}",
                    file=sys.stderr,
                )
            continue
        item = card_to_item(token, c)
        item["sizeX"] = int(dc.get("size_x", 4))
        item["sizeY"] = int(dc.get("size_y", 4))
        item["row"] = int(dc.get("row", 0))
        item["col"] = int(dc.get("col", 0))
        out["cards"].append(item)
    return out


def find_dashboard_id(token: str, want_prefix: str) -> int | None:
    u = api("/api/user/current", "GET", None, token)
    personal = u.get("personal_collection_id")
    if not personal:
        return None
    got = api(f"/api/collection/{personal}/items", "GET", None, token)
    items: list
    if isinstance(got, dict) and "data" in got:
        items = got["data"] or []
    else:
        items = got or []
    for it in items:
        if it.get("model") == "dashboard":
            name = it.get("name") or ""
            if name.strip().startswith(want_prefix):
                return int(it["id"])
    return None


def main() -> int:
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    out_dir = os.path.join(root, "metabase_dashboards")
    try:
        token = api("/api/session", "POST", {"username": USER, "password": PASS})["id"]
    except Exception as e:
        print(f"Cannot log in to Metabase at {MB}: {e}", file=sys.stderr)
        return 1

    targets: list[tuple[str, str]] = [
        ("01 Оперативный", "01_operational.json"),
        ("09 Управленческ", "09_executive.json"),  # prefix: "09 Управленческий"
    ]
    for prefix, filename in targets:
        did = find_dashboard_id(token, prefix)
        if not did and prefix.startswith("09"):
            did = find_dashboard_id(token, "09 Управленч")
        if not did and prefix.startswith("01"):
            did = find_dashboard_id(token, "01")
        if not did:
            print(f"ERROR: no dashboard for prefix {prefix!r}", file=sys.stderr)
            return 1
        print(f"Exporting dashboard_id={did} -> {filename}")
        data = export_dashboard(token, did)
        full = os.path.join(out_dir, filename)
        with open(full, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
            f.write("\n")
        print(f"  OK {len(data['cards'])} cards -> {full}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
