"""
Parse EGISZ SOAP callback fragments from EXCHANGELOG.MSGTEXT (XML body).

LOGTEXT typically holds the clinic endpoint URL (gost-…); MSGTEXT holds the SOAP/XML payload.
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


def _soap_xml_source(msg_text: str | None) -> str | None:
    """SOAP/XML только из EXCHANGELOG.MSGTEXT; в LOGTEXT — только транспортный хост, не XML."""
    blob = (msg_text or "").strip()
    if not blob or "<" not in blob:
        return None
    return blob


def _extract_embedded_xml(raw: str) -> str:
    """MSGTEXT may prefix transport lines before the SOAP document."""
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
    local_uid_semd: str | None
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
            "local_uid_semd": self.local_uid_semd,
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
    SOAP-focused parser: XML from MSGTEXT; gost- host from LOGTEXT then EGISZ_MESSAGES.REPLYTO.
    Clinic JID: LOGTEXT URL, строка EGISZ_LICENSES (JID), или OID→EGISZ_LICENSES.MO_UID→JID; реквизиты МО из JPERSONS.
    """

    def __init__(self, log_excerpt_max: int = 4000) -> None:
        self.log_excerpt_max = log_excerpt_max

    def extract_jid(
        self,
        log_text: str | None,
        reply_to: str | None = None,
    ) -> dict[str, Any]:
        """
        Extract clinic token from gost-<jid>.infoclinica.lan in LOGTEXT, then in REPLYTO.

        DOCUMENTID / localUid are not used here; they feed local_uid_semd only after SOAP localUid is read (see build_record).
        """
        def _first_gost(s: str | None) -> tuple[str | None, int | None]:
            if not s:
                return None, None
            best: str | None = None
            for m in _GOST_JID_RE.finditer(s):
                token = m.group(1).lower()
                if best is None:
                    best = token
            if not best:
                return None, None
            jid: int | None = int(best) if best.isdigit() else None
            return best, jid

        for blob in (log_text, reply_to):
            token, jid = _first_gost(blob)
            if token is not None:
                return {"jid": jid, "gost_jid_token": token}

        return {"jid": None, "gost_jid_token": None}

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
        local_uid: str | None = None
        status: str | None = None
        kind: str | None = None
        org_oid: str | None = None
        emdr_id: str | None = None
        reg_date: str | None = None
        errors: list[dict[str, str]] = []

        stack: list[str] = []

        def walk(el: ET.Element) -> None:
            nonlocal relates, local_uid, status, kind, org_oid, emdr_id, reg_date, errors
            stack.append(_local_tag(el.tag))
            path = stack
            text = _norm_ws(el.text)
            ln = path[-1]

            if ln == "relatesToMessage" and text:
                relates = text
            elif ln == "localUid" and text:
                local_uid = text
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
                "local_uid": _norm_ws(local_uid),
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
            "local_uid": _norm_ws(local_uid),
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
        jid_from_egisz_licenses_row: int | None = None,
        jid_by_mo_uid_from_egisz_licenses: Mapping[str, int] | None = None,
    ) -> tuple[int | None, str | None]:
        """
        Resolve internal JID: URL → EGISZ_LICENSES.JID из строки выборки → OID→EGISZ_LICENSES.MO_UID→JID.
        """
        if jid_from_url is not None and jid_from_url > 0:
            return jid_from_url, None

        lj = jid_from_egisz_licenses_row
        if lj is not None and lj > 0:
            return int(lj), None

        oid_n = _norm_ws(oid)
        if oid_n and jid_by_mo_uid_from_egisz_licenses:
            mapped = jid_by_mo_uid_from_egisz_licenses.get(oid_n)
            if mapped is not None and mapped > 0:
                return mapped, oid_n

        return None, oid_n

    def build_record(
        self,
        log_text: str | None,
        *,
        msg_text: str | None = None,
        kind_from_egisz_licenses: str | int | None = None,
        mo_uid_from_egisz_licenses: str | None = None,
        jid_from_egisz_licenses_row: int | None = None,
        jid_by_mo_uid_from_egisz_licenses: Mapping[str, int] | None = None,
        reply_to: str | None = None,
        document_id: str | None = None,
        on_staging_error: Callable[[StagingParseError], None] | None = None,
    ) -> NormalizedRecord | None:
        """
        SOAP только из MSGTEXT; хост gost- из LOGTEXT затем REPLYTO.
        KIND из XML (MSGTEXT) либо из колонки EGISZ_LICENSES.KIND строки журнала. UPSERT key: relates_to_id.
        local_uid_semd: тег localUid в SOAP либо EGISZ_MESSAGES.DOCUMENTID.
        """
        combined = "\n".join(
            x for x in ((msg_text or "").strip(), (log_text or "").strip()) if x
        )
        excerpt = combined[: self.log_excerpt_max] if combined else ""

        host_part = self.extract_jid(log_text, reply_to=reply_to)
        jid_url = host_part["jid"]
        gost_token = host_part["gost_jid_token"]

        soap_src = _soap_xml_source(msg_text)
        parsed = self.parse_xml(soap_src)

        relates_to_id: str | None = None
        status = "unknown"
        kind_code: str | None = None
        org_oid: str | None = None
        emdr_id: str | None = None
        registration_date: datetime | None = None
        errors_json: list[dict[str, str]] = []
        local_uid_xml: str | None = None

        if parsed:
            relates_to_id = parsed.get("relates_to_id")
            status = parsed.get("status") or "unknown"
            kind_code = parsed.get("kind_code")
            org_oid = parsed.get("org_oid")
            emdr_id = parsed.get("emdr_id")
            registration_date = parsed.get("registration_date")
            errors_json = list(parsed.get("errors") or [])
            local_uid_xml = parsed.get("local_uid")

        if not kind_code:
            kind_code = _norm_kind_code(
                str(kind_from_egisz_licenses).strip() if kind_from_egisz_licenses is not None else None
            )

        mo_uid_egisz = _norm_ws(mo_uid_from_egisz_licenses)
        org_for_resolve = org_oid or mo_uid_egisz

        kind_name = get_semd_name(kind_code) if kind_code else get_semd_name(None)

        jid_resolved, oid_kept = self.resolve_clinic(
            jid_url,
            org_for_resolve,
            jid_from_egisz_licenses_row=jid_from_egisz_licenses_row,
            jid_by_mo_uid_from_egisz_licenses=jid_by_mo_uid_from_egisz_licenses,
        )
        org_out = org_oid or mo_uid_egisz or oid_kept

        local_uid_semd = _norm_ws(local_uid_xml) or _norm_ws(document_id)

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
                        log_excerpt=excerpt or None,
                    )
                )
            elif parsed is None and "relatesToMessage" in (msg_text or ""):
                _stage(
                    StagingParseError(
                        relates_to_id=None,
                        error_code="XML_BROKEN",
                        message="relatesToMessage hinted in text but XML not parseable",
                        log_excerpt=excerpt or None,
                    )
                )
            return None

        return NormalizedRecord(
            relates_to_id=relates_to_id,
            local_uid_semd=local_uid_semd,
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
