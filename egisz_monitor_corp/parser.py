"""
Parse EGISZ SOAP callback fragments from EXCHANGELOG.LOGTEXT.

Namespace (canonical): ns2 = http://egisz.rosminzdrav.ru/iehr/emdr/callback/
Tags may use any prefix; matching uses local-name (Clark notation).
"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Mapping

from egisz_monitor_corp.semd_dictionary import get_semd_name

# Primary id from callback (async link to outbound request).
_RELATES_RE = re.compile(
    r"<[^>:]*:?relatesToMessage[^>]*>([^<]+)</[^>:]*:?relatesToMessage>",
    re.IGNORECASE | re.DOTALL,
)

# gost-<jid>.infoclinica.lan — port ignored when matching full URL in text.
_GOST_JID_RE = re.compile(
    r"gost-([a-zA-Z0-9_-]+)\.infoclinica\.lan",
    re.IGNORECASE,
)


def _local_tag(tag: str) -> str:
    if "}" in tag:
        return tag.rsplit("}", 1)[-1]
    return tag


def _norm_ws(s: str | None) -> str | None:
    if s is None:
        return None
    t = s.strip()
    return t or None


def _extract_embedded_xml(raw: str) -> str:
    """LOGTEXT often prefixes transport lines before the SOAP document."""
    markers = ("<?xml", "<soap:", "<SOAP:", "<soap ", "<SOAP ", "<s:Envelope", "<S:Envelope")
    positions = [raw.find(m) for m in markers if raw.find(m) != -1]
    if positions:
        return raw[min(positions) :]
    idx = raw.find("<")
    return raw[idx:] if idx != -1 else raw


def _norm_kind_code(raw: str | None) -> str | None:
    if not raw:
        return None
    t = raw.strip()
    if not t:
        return None
    if re.fullmatch(r"\d{1,4}", t):
        return str(int(t))
    return t


@dataclass
class StagingParseError:
    relates_to_id: str | None
    error_code: str
    message: str
    log_excerpt: str | None = None


@dataclass
class NormalizedRecord:
    """Row-shaped payload for fact_egisz_transactions (before UPSERT)."""

    relates_to_id: str
    jid: int | None
    gost_jid_token: str | None
    org_oid: str | None
    kind_code: str | None
    kind_name: str | None
    status: str
    emdr_id: str | None
    errors_json: list[dict[str, str]]
    registration_date: datetime | None
    processed_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    def as_fact_row(self) -> dict[str, Any]:
        return {
            "relates_to_id": self.relates_to_id,
            "jid": self.jid,
            "gost_jid_token": self.gost_jid_token,
            "org_oid": self.org_oid,
            "kind_code": self.kind_code,
            "status": self.status,
            "emdr_id": self.emdr_id,
            "errors_json": self.errors_json,
            "registration_date": self.registration_date,
            "processed_at": self.processed_at,
        }


class EgiszMonitorParser:
    """
    SOAP-focused parser with resilient XML handling and clinic resolution chain:
    JID: gost- URL → LICENSE_JID из строки Firebird → OID → license map → JPERSONS.
    """

    def __init__(self, log_excerpt_max: int = 4000) -> None:
        self.log_excerpt_max = log_excerpt_max

    def extract_jid(self, log_text: str | None) -> dict[str, Any]:
        """
        Extract clinic token from gost-<jid>.infoclinica.lan URLs inside LOGTEXT.

        Returns:
            jid: int | None — when the token is all digits (internal JID).
            gost_jid_token: str | None — lowercased token from the host segment.
        """
        if not log_text:
            return {"jid": None, "gost_jid_token": None}

        best_token: str | None = None
        for m in _GOST_JID_RE.finditer(log_text):
            token = m.group(1).lower()
            # Deterministic: first gost-… host in log order (avoids picking unrelated long tokens).
            if best_token is None:
                best_token = token

        if not best_token:
            return {"jid": None, "gost_jid_token": None}

        jid: int | None = None
        if best_token.isdigit():
            jid = int(best_token)

        return {"jid": jid, "gost_jid_token": best_token}

    def parse_xml(self, xml_string: str | None) -> dict[str, Any] | None:
        """
        Lazy-friendly: returns None if there is no parseable SOAP fragment.

        May return dict with relates_to_id None — caller should stage and skip fact UPSERT.
        """
        if not xml_string or "<" not in xml_string:
            return None

        lowered = xml_string.lower()
        if "registerdocumentresult" not in lowered and "relatestomessage" not in lowered:
            return None

        payload = _extract_embedded_xml(xml_string)
        try:
            root = ET.fromstring(payload)
        except ET.ParseError:
            try:
                root = ET.fromstring(f"<root>{payload}</root>")
            except ET.ParseError:
                return None

        relates: str | None = None
        status: str | None = None
        kind: str | None = None
        org_oid: str | None = None
        emdr_id: str | None = None
        reg_date: str | None = None
        errors: list[dict[str, str]] = []

        stack: list[str] = []

        def walk(el: ET.Element) -> None:
            nonlocal relates, status, kind, org_oid, emdr_id, reg_date, errors
            stack.append(_local_tag(el.tag))
            path = stack
            text = _norm_ws(el.text)
            ln = path[-1]

            if ln == "relatesToMessage" and text:
                relates = text
            elif ln == "status" and text and "registerDocumentResult" in path:
                status = text.lower()
            elif ln == "kind" and text:
                kind = text
            elif ln == "organization" and text:
                org_oid = text
            elif ln == "emdrId" and text:
                emdr_id = text
            elif ln == "registrationDate" and text:
                reg_date = text
            elif ln == "item" and "errors" in path:
                code = None
                message = None
                for child in el:
                    cn = _local_tag(child.tag)
                    cv = _norm_ws(child.text)
                    if cn == "code" and cv:
                        code = cv
                    elif cn == "message" and cv:
                        message = cv
                if code or message:
                    errors.append({"code": code or "", "message": message or ""})

            for ch in list(el):
                walk(ch)
            stack.pop()

        walk(root)

        if not relates:
            m = _RELATES_RE.search(payload)
            if m:
                relates = _norm_ws(m.group(1))

        reg_dt_raw = _norm_ws(reg_date)
        reg_dt = _parse_iso_datetime(reg_dt_raw) if reg_dt_raw else None

        if not relates:
            return {
                "relates_to_id": None,
                "status": status or "unknown",
                "kind_code": _norm_kind_code(kind),
                "org_oid": org_oid,
                "emdr_id": emdr_id,
                "registration_date": reg_dt,
                "errors": errors,
                "_xml_ok": True,
            }

        st = status or ("error" if errors else "unknown")
        if st not in ("success", "error", "unknown"):
            st = "unknown"

        return {
            "relates_to_id": relates,
            "status": st,
            "kind_code": _norm_kind_code(kind),
            "org_oid": org_oid,
            "emdr_id": emdr_id,
            "registration_date": reg_dt,
            "errors": errors,
            "_xml_ok": True,
        }

    def resolve_clinic(
        self,
        jid_from_url: int | None,
        oid: str | None,
        *,
        license_jid_from_row: int | None = None,
        license_jid_by_mo_uid: Mapping[str, int] | None = None,
    ) -> tuple[int | None, str | None]:
        """
        Resolve internal JID: URL → EGISZ_LICENSES.JID from extraction row → OID map.
        """
        if jid_from_url is not None and jid_from_url > 0:
            return jid_from_url, None

        lj = license_jid_from_row
        if lj is not None and lj > 0:
            return int(lj), None

        oid_n = _norm_ws(oid)
        if oid_n and license_jid_by_mo_uid:
            mapped = license_jid_by_mo_uid.get(oid_n)
            if mapped is not None and mapped > 0:
                return mapped, oid_n

        return None, oid_n

    def build_record(
        self,
        log_text: str | None,
        *,
        kind_from_licenses: str | int | None = None,
        org_from_licenses: str | None = None,
        license_jid_from_row: int | None = None,
        license_jid_by_mo_uid: Mapping[str, int] | None = None,
        on_staging_error: Callable[[StagingParseError], None] | None = None,
    ) -> NormalizedRecord | None:
        """
        Parse LOGTEXT; KIND only from XML then EGISZ_LICENSES (KIND is not on messages).
        MO_UID from licenses used when SOAP omits organization. Resolve JID. UPSERT: relates_to_id.
        """
        excerpt = (log_text or "")[: self.log_excerpt_max]

        host_part = self.extract_jid(log_text)
        jid_url = host_part["jid"]
        gost_token = host_part["gost_jid_token"]

        parsed = self.parse_xml(log_text)

        relates_to_id: str | None = None
        status = "unknown"
        kind_code: str | None = None
        org_oid: str | None = None
        emdr_id: str | None = None
        registration_date: datetime | None = None
        errors_json: list[dict[str, str]] = []

        if parsed:
            relates_to_id = parsed.get("relates_to_id")
            status = parsed.get("status") or "unknown"
            kind_code = parsed.get("kind_code")
            org_oid = parsed.get("org_oid")
            emdr_id = parsed.get("emdr_id")
            registration_date = parsed.get("registration_date")
            errors_json = list(parsed.get("errors") or [])

        if not kind_code:
            kind_code = _norm_kind_code(
                str(kind_from_licenses).strip() if kind_from_licenses is not None else None
            )

        org_license = _norm_ws(org_from_licenses)
        org_for_resolve = org_oid or org_license

        kind_name = get_semd_name(kind_code) if kind_code else get_semd_name(None)

        jid_resolved, oid_kept = self.resolve_clinic(
            jid_url,
            org_for_resolve,
            license_jid_from_row=license_jid_from_row,
            license_jid_by_mo_uid=license_jid_by_mo_uid,
        )
        org_out = org_oid or org_license or oid_kept

        def _stage(err: StagingParseError) -> None:
            if on_staging_error:
                on_staging_error(err)

        if not relates_to_id:
            if parsed and parsed.get("_xml_ok") and parsed.get("relates_to_id") is None:
                _stage(
                    StagingParseError(
                        relates_to_id=None,
                        error_code="MISSING_RELATES_TO",
                        message="SOAP fragment without relatesToMessage",
                        log_excerpt=excerpt,
                    )
                )
            elif parsed is None and "relatesToMessage" in (log_text or ""):
                _stage(
                    StagingParseError(
                        relates_to_id=None,
                        error_code="XML_BROKEN",
                        message="relatesToMessage hinted in text but XML not parseable",
                        log_excerpt=excerpt,
                    )
                )
            return None

        return NormalizedRecord(
            relates_to_id=relates_to_id,
            jid=jid_resolved,
            gost_jid_token=gost_token,
            org_oid=org_out,
            kind_code=kind_code,
            kind_name=kind_name,
            status=status,
            emdr_id=emdr_id,
            errors_json=errors_json,
            registration_date=registration_date,
        )


def _parse_iso_datetime(raw: str) -> datetime | None:
    if not raw:
        return None
    try:
        if raw.endswith("Z"):
            raw = raw[:-1] + "+00:00"
        dt = datetime.fromisoformat(raw)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except ValueError:
        return None
