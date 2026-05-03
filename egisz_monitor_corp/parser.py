"""
Parse EGISZ SOAP callback fragments from EXCHANGELOG.MSGTEXT (XML body).

LOGTEXT typically holds the clinic endpoint URL (gost-…); MSGTEXT holds the SOAP/XML payload.
Namespace (canonical): ns2 = http://egisz.rosminzdrav.ru/iehr/emdr/callback/
Tags may use any prefix; matching uses local-name (Clark notation).

Парсинг MSGTEXT: `defusedxml.ElementTree` (запрет DTD/внешних сущностей, см. docs/GORDON2_XML_PROMPT.md).
"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Mapping

from defusedxml.ElementTree import fromstring as _xml_fromstring
from defusedxml.common import DefusedXmlException

from egisz_monitor_corp.semd_dictionary import get_semd_name

# Primary id from callback (async link to outbound request).
_RELATES_RE = re.compile(
    r"<[^>:]*:?relatesToMessage[^>]*>([^<]+)</[^>:]*:?relatesToMessage>",
    re.IGNORECASE | re.DOTALL,
)
# When XML is not parseable, these regexes still pick identifiers from raw MSGTEXT (same local-name style).
_LOCALUID_RE = re.compile(
    r"<[^>:]*:?localUid[^>]*>([^<]+)</[^>:]*:?localUid>",
    re.IGNORECASE | re.DOTALL,
)
_EMDRID_RE = re.compile(
    r"<[^>:]*:?emdrId[^>]*>([^<]+)</[^>:]*:?emdrId>",
    re.IGNORECASE | re.DOTALL,
)
_KIND_RE = re.compile(
    r"<[^>:]*:?kind[^>]*>([^<]+)</[^>:]*:?kind>",
    re.IGNORECASE | re.DOTALL,
)

# Max bytes scanned for hint regexes (very large MSGTEXT: first embedded SOAP chunk only).
_HINT_SCAN_MAX = 500_000

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
    start = -1
    for m in markers:
        i = raw.find(m)
        if i != -1 and (start == -1 or i < start):
            start = i
    if start != -1:
        return raw[start:]
    idx = raw.find("<")
    return raw[idx:] if idx != -1 else raw


def extract_parse_hints(msg_text: str | None) -> tuple[str | None, str | None, str | None]:
    """
    Best-effort identifiers from raw MSGTEXT when ElementTree fails (XML_BROKEN / staging).

    Order of precedence for grouping in reporting: relatesToMessage → localUid → emdrId
    (aligned with fact key relates_to_id and СЭМД instance ids in `.cursorrules`).
    """
    blob = (msg_text or "").strip()
    if not blob:
        return None, None, None
    if len(blob) > _HINT_SCAN_MAX:
        blob = _extract_embedded_xml(blob)[:_HINT_SCAN_MAX]

    def first(pat: re.Pattern[str], s: str) -> str | None:
        m = pat.search(s)
        return _norm_ws(m.group(1)) if m else None

    return (first(_RELATES_RE, blob), first(_LOCALUID_RE, blob), first(_EMDRID_RE, blob))


def _jid_sources_mismatch(
    jid_license: int | None,
    jid_gost_log: int | None,
    jid_gost_reply: int | None,
    token_log: str | None,
    token_reply: str | None,
) -> bool:
    """True if EGISZ_LICENSES.JID vs gost in LOGTEXT vs gost in REPLYTO disagree (numeric or token)."""
    nums: list[int] = []
    if jid_license is not None and jid_license > 0:
        nums.append(int(jid_license))
    if jid_gost_log is not None and jid_gost_log > 0:
        nums.append(int(jid_gost_log))
    if jid_gost_reply is not None and jid_gost_reply > 0:
        nums.append(int(jid_gost_reply))
    if len(set(nums)) > 1:
        return True
    tl = (token_log or "").strip().lower()
    tr = (token_reply or "").strip().lower()
    if tl and tr and tl != tr:
        return True
    for tok in (tl, tr):
        if tok and not tok.isdigit() and len(nums) >= 1:
            return True
    return False


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
    exchangelog_log_id: int | None = None
    egisz_messages_egmid: int | None = None
    journal_msgid: str | None = None
    relates_to_hint: str | None = None
    local_uid_hint: str | None = None
    emdr_id_hint: str | None = None

    def as_insert_tuple(
        self,
    ) -> tuple[
        str | None,
        str,
        str,
        str | None,
        int | None,
        int | None,
        str | None,
        str | None,
        str | None,
        str | None,
    ]:
        return (
            self.relates_to_id,
            self.error_code,
            self.message,
            self.log_excerpt,
            self.exchangelog_log_id,
            self.egisz_messages_egmid,
            self.journal_msgid,
            self.relates_to_hint,
            self.local_uid_hint,
            self.emdr_id_hint,
        )


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
    semd_creation_at: datetime | None = None
    processed_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    exchangelog_log_id: int | None = None
    egisz_messages_egmid: int | None = None
    jid_from_license: int | None = None
    jid_from_gost_log: int | None = None
    jid_from_gost_reply: int | None = None
    gost_token_logtext: str | None = None
    gost_token_replyto: str | None = None
    jid_sources_mismatch: bool = False

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
            "semd_creation_at": self.semd_creation_at,
            "processed_at": self.processed_at,
            "exchangelog_log_id": self.exchangelog_log_id,
            "egisz_messages_egmid": self.egisz_messages_egmid,
            "jid_from_license": self.jid_from_license,
            "jid_from_gost_log": self.jid_from_gost_log,
            "jid_from_gost_reply": self.jid_from_gost_reply,
            "gost_token_logtext": self.gost_token_logtext,
            "gost_token_replyto": self.gost_token_replyto,
            "jid_sources_mismatch": self.jid_sources_mismatch,
        }


class EgiszMonitorParser:
    """
    SOAP-focused parser: XML from MSGTEXT; клиника **gost-…** только из **LOGTEXT** и **REPLYTO** (не из MSGTEXT).
    Итоговый JID: resolve_clinic(gost из транспорта, лицензия по REPLYTO, OID); реквизиты МО из JPERSONS.
    """

    def __init__(self, log_excerpt_max: int = 4000) -> None:
        self.log_excerpt_max = log_excerpt_max

    def extract_jid(
        self,
        log_text: str | None,
        reply_to: str | None = None,
        msg_text: str | None = None,
    ) -> dict[str, Any]:
        """
        gost-<token>.infoclinica.lan отдельно в **LOGTEXT** и **REPLYTO**. MSGTEXT для JID не используется.

        Возвращает числовые JID по каждому каналу, токены (в т.ч. нецифровые для отображения) и ``jid_url``:
        первое числовое значение — из LOGTEXT, иначе из REPLYTO (для resolve_clinic).
        ``gost_jid_token`` — нецифровой токен для колонки факта (приоритет LOGTEXT, затем REPLYTO), иначе None.
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

        _ = msg_text  # намеренно не используется: JID не берётся из тела SOAP

        token_log, jid_log = _first_gost(log_text)
        token_reply, jid_reply = _first_gost(reply_to)

        jid_url = jid_log if jid_log is not None and jid_log > 0 else (jid_reply if jid_reply is not None and jid_reply > 0 else None)

        gost_jid_token: str | None = None
        if token_log and not token_log.isdigit():
            gost_jid_token = token_log
        elif token_reply and not token_reply.isdigit():
            gost_jid_token = token_reply

        return {
            "jid_gost_log": jid_log,
            "gost_token_log": token_log,
            "jid_gost_reply": jid_reply,
            "gost_token_reply": token_reply,
            "jid_url": jid_url,
            "gost_jid_token": gost_jid_token,
        }

    def parse_xml(self, xml_string: str | None) -> dict[str, Any] | None:
        """
        Lazy-friendly: returns None if there is no parseable SOAP fragment.

        May return dict with relates_to_id None — caller should stage and skip fact UPSERT.
        """
        if not xml_string or "<" not in xml_string:
            return None

        # Без полного .lower() по крупному BLOB: один проход регистронезависимого поиска.
        if re.search(r"registerdocumentresult|relatestomessage", xml_string, re.IGNORECASE) is None:
            return None

        payload = _extract_embedded_xml(xml_string)
        try:
            root = _xml_fromstring(payload)
        except (ET.ParseError, DefusedXmlException):
            try:
                root = _xml_fromstring(f"<root>{payload}</root>")
            except (ET.ParseError, DefusedXmlException):
                return None

        relates: str | None = None
        local_uid: str | None = None
        status: str | None = None
        kind: str | None = None
        org_oid: str | None = None
        emdr_id: str | None = None
        reg_date: str | None = None
        reg_date_time: str | None = None
        cre_date: str | None = None
        errors: list[dict[str, str]] = []

        stack: list[str] = []

        def walk(el: ET.Element) -> None:
            nonlocal relates, local_uid, status, kind, org_oid, emdr_id, reg_date, reg_date_time, cre_date, errors
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
            elif ln == "kind":
                merged = _norm_ws("".join(el.itertext()))
                if merged:
                    kind = merged
            elif ln == "organization" and text:
                org_oid = text
            elif ln == "emdrId" and text:
                emdr_id = text
            elif ln == "registrationDateTime" and text:
                reg_date_time = text
            elif ln == "registrationDate" and text:
                reg_date = text
            elif ln == "creationDateTime" and text:
                cre_date = text
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

        if not kind:
            km = _KIND_RE.search(payload)
            if km:
                kind = _norm_ws(km.group(1))

        if not relates:
            m = _RELATES_RE.search(payload)
            if m:
                relates = _norm_ws(m.group(1))

        reg_dt_raw = _norm_ws(reg_date_time) or _norm_ws(reg_date)
        reg_dt = _parse_iso_datetime(reg_dt_raw) if reg_dt_raw else None
        cre_dt_raw = _norm_ws(cre_date)
        cre_dt = _parse_iso_datetime(cre_dt_raw) if cre_dt_raw else None

        if not relates:
            return {
                "relates_to_id": None,
                "local_uid": _norm_ws(local_uid),
                "status": status or "unknown",
                "kind_code": _norm_kind_code(kind),
                "org_oid": org_oid,
                "emdr_id": emdr_id,
                "registration_date": reg_dt,
                "semd_creation_at": cre_dt,
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
            "semd_creation_at": cre_dt,
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
        Resolve internal JID: gost (числовой) из **LOGTEXT**/**REPLYTO** → EGISZ_LICENSES.JID из строки выборки → OID→EGISZ_LICENSES.MO_UID→JID.
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
        msg_created_at: datetime | None = None,
        log_created_at: datetime | None = None,
        on_staging_error: Callable[[StagingParseError], None] | None = None,
        exchangelog_log_id: int | None = None,
        egisz_messages_egmid: int | None = None,
        journal_msgid: str | None = None,
    ) -> NormalizedRecord | None:
        """
        SOAP только из MSGTEXT; **gost-** для JID только из **LOGTEXT** и **REPLYTO** (не из MSGTEXT).
        KIND из XML (MSGTEXT) либо из колонки EGISZ_LICENSES.KIND строки журнала. UPSERT key: relates_to_id.
        local_uid_semd: тег localUid в SOAP либо EGISZ_MESSAGES.DOCUMENTID.
        """
        combined = "\n".join(
            x for x in ((msg_text or "").strip(), (log_text or "").strip()) if x
        )
        excerpt = combined[: self.log_excerpt_max] if combined else ""

        host_part = self.extract_jid(log_text, reply_to=reply_to, msg_text=msg_text)
        jid_url = host_part["jid_url"]
        gost_token = host_part["gost_jid_token"]
        t_log = host_part["gost_token_log"]
        t_rep = host_part["gost_token_reply"]
        j_log = host_part["jid_gost_log"]
        j_rep = host_part["jid_gost_reply"]

        soap_src = _soap_xml_source(msg_text)
        parsed = self.parse_xml(soap_src)

        relates_to_id: str | None = None
        status = "unknown"
        kind_code: str | None = None
        org_oid: str | None = None
        emdr_id: str | None = None
        registration_date: datetime | None = None
        semd_creation_at: datetime | None = None
        errors_json: list[dict[str, str]] = []
        local_uid_xml: str | None = None

        if parsed:
            relates_to_id = parsed.get("relates_to_id")
            status = parsed.get("status") or "unknown"
            kind_code = parsed.get("kind_code")
            org_oid = parsed.get("org_oid")
            emdr_id = parsed.get("emdr_id")
            registration_date = parsed.get("registration_date")
            semd_creation_at = parsed.get("semd_creation_at")
            errors_json = list(parsed.get("errors") or [])
            local_uid_xml = parsed.get("local_uid")

        processed_at = msg_created_at or log_created_at or datetime.now(timezone.utc)

        if not kind_code:
            kind_code = _norm_kind_code(
                str(kind_from_egisz_licenses).strip() if kind_from_egisz_licenses is not None else None
            )

        mo_uid_egisz = _norm_ws(mo_uid_from_egisz_licenses)
        org_for_resolve = org_oid or mo_uid_egisz

        lic_jid: int | None = None
        if jid_from_egisz_licenses_row is not None:
            try:
                lj = int(jid_from_egisz_licenses_row)
                if lj > 0:
                    lic_jid = lj
            except (TypeError, ValueError):
                pass

        kind_name = get_semd_name(kind_code) if kind_code else get_semd_name(None)

        jid_resolved, oid_kept = self.resolve_clinic(
            jid_url,
            org_for_resolve,
            jid_from_egisz_licenses_row=jid_from_egisz_licenses_row,
            jid_by_mo_uid_from_egisz_licenses=jid_by_mo_uid_from_egisz_licenses,
        )
        org_out = org_oid or mo_uid_egisz or oid_kept

        mismatch = _jid_sources_mismatch(lic_jid, j_log, j_rep, t_log, t_rep)

        local_uid_semd = _norm_ws(local_uid_xml) or _norm_ws(document_id)

        hint_relates, hint_local, hint_emdr = extract_parse_hints(soap_src)

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
                        exchangelog_log_id=exchangelog_log_id,
                        egisz_messages_egmid=egisz_messages_egmid,
                        journal_msgid=journal_msgid,
                        relates_to_hint=hint_relates,
                        local_uid_hint=hint_local,
                        emdr_id_hint=hint_emdr,
                    )
                )
            elif parsed is None and "relatesToMessage" in (msg_text or ""):
                _stage(
                    StagingParseError(
                        relates_to_id=None,
                        error_code="XML_BROKEN",
                        message="relatesToMessage hinted in text but XML not parseable",
                        log_excerpt=excerpt or None,
                        exchangelog_log_id=exchangelog_log_id,
                        egisz_messages_egmid=egisz_messages_egmid,
                        journal_msgid=journal_msgid,
                        relates_to_hint=hint_relates,
                        local_uid_hint=hint_local,
                        emdr_id_hint=hint_emdr,
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
            semd_creation_at=semd_creation_at,
            processed_at=processed_at,
            exchangelog_log_id=exchangelog_log_id,
            egisz_messages_egmid=egisz_messages_egmid,
            jid_from_license=lic_jid,
            jid_from_gost_log=j_log if j_log is not None and j_log > 0 else None,
            jid_from_gost_reply=j_rep if j_rep is not None and j_rep > 0 else None,
            gost_token_logtext=t_log,
            gost_token_replyto=t_rep,
            jid_sources_mismatch=mismatch,
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
