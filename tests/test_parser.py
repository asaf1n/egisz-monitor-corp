"""Tests for EgiszMonitorParser."""

from __future__ import annotations

from decimal import Decimal

from egisz_monitor_corp.parser import EgiszMonitorParser, coerce_exchangelog_log_state, extract_parse_hints


NS = "http://egisz.rosminzdrav.ru/iehr/emdr/callback/"


def _soap(
    relates: str,
    status: str,
    *,
    kind: str | None = "<ns2:kind>62</ns2:kind>",
    org: str | None = "<ns2:organization>1.2.643.5.1.13.13.99.99</ns2:organization>",
    errors_block: str = "",
    success_block: str = "",
) -> str:
    kind_xml = kind or ""
    org_xml = org or ""
    return f"""<?xml version="1.0"?>
<soap:Envelope xmlns:soap="http://schemas.xmlsoap.org/soap/envelope/" xmlns:ns2="{NS}">
  <soap:Body>
    <ns2:registerDocumentResult>
      <ns2:relatesToMessage>{relates}</ns2:relatesToMessage>
      <ns2:status>{status}</ns2:status>
      {kind_xml}
      {org_xml}
      {success_block}
      {errors_block}
    </ns2:registerDocumentResult>
  </soap:Body>
</soap:Envelope>"""


def test_extract_jid_from_url_ignores_port() -> None:
    p = EgiszMonitorParser()
    log = "callback http://GOST-42.infoclinica.lan:8080/foo extra"
    r = p.extract_jid(log)
    assert r["jid_url"] == 42
    assert r["jid_gost_log"] == 42
    assert r["gost_jid_token"] is None
    assert r["gost_token_log"] == "42"


def test_extract_jid_alphanumeric_token_no_int_jid() -> None:
    p = EgiszMonitorParser()
    log = "http://gost-clinic-a.infoclinica.lan:443/"
    r = p.extract_jid(log)
    assert r["jid_url"] is None
    assert r["gost_jid_token"] == "clinic-a"
    assert r["gost_token_log"] == "clinic-a"


def test_extract_jid_falls_back_to_reply_to_when_log_has_no_gost() -> None:
    p = EgiszMonitorParser()
    log = "no gost host here"
    reply = "https://gost-99.infoclinica.lan/soap"
    r = p.extract_jid(log, reply_to=reply)
    assert r["jid_url"] == 99
    assert r["jid_gost_reply"] == 99
    assert r["gost_jid_token"] is None


def test_extract_jid_ignores_msgtext_for_gost() -> None:
    """MSGTEXT не участвует в извлечении gost; только LOGTEXT затем число из REPLYTO как запасной jid_url."""
    p = EgiszMonitorParser()
    log = "http://gost-1.infoclinica.lan/"
    reply = "http://gost-2.infoclinica.lan/"
    msg = "echo http://gost-88.infoclinica.lan/ in payload"
    r = p.extract_jid(log, reply_to=reply, msg_text=msg)
    assert r["jid_url"] == 1
    assert r["jid_gost_log"] == 1
    assert r["jid_gost_reply"] == 2


def test_build_record_uses_reply_to_for_jid_when_logtext_plain() -> None:
    p = EgiszMonitorParser()
    xml = _soap("MSG-RT", "success", kind="<ns2:kind>62</ns2:kind>")
    log = "plain transport text without gost host"
    reply = "http://gost-5.infoclinica.lan/callback"
    rec = p.build_record(
        log,
        msg_text=xml,
        reply_to=reply,
        document_id="local-uid-1",
        kind_from_egisz_licenses="62",
    )
    assert rec is not None
    assert rec.jid == 5
    assert rec.gost_jid_token is None


def test_soap_not_parsed_from_logtext_xml_must_be_msgtext() -> None:
    """LOGTEXT never carries SOAP; XML only in MSGTEXT."""
    p = EgiszMonitorParser()
    xml = _soap("MSG-ONLY-MSG", "success", kind="<ns2:kind>62</ns2:kind>")
    log = "http://gost-3.infoclinica.lan/"
    rec = p.build_record(log, msg_text=None, document_id="x")
    assert rec is None
    rec2 = p.build_record(log, msg_text=xml, document_id="DOC-FALLBACK")
    assert rec2 is not None
    assert rec2.relates_to_id == "MSG-ONLY-MSG"


def test_build_record_prefers_msgtext_for_soap_logtext_host_only() -> None:
    """Production layout: MSGTEXT = SOAP, LOGTEXT = gost- URL only."""
    p = EgiszMonitorParser()
    xml = _soap("MSG-HOST", "success", kind="<ns2:kind>62</ns2:kind>")
    log = "http://gost-12.infoclinica.lan:9945/callback"
    rec = p.build_record(log, msg_text=xml, reply_to=None, document_id="DOC-99", kind_from_egisz_licenses="62")
    assert rec is not None
    assert rec.relates_to_id == "MSG-HOST"
    assert rec.jid == 12
    assert rec.local_uid_semd == "DOC-99"


def test_build_record_gost_from_logtext_only_msgtext_extra_ignored() -> None:
    """Тело SOAP (MSGTEXT) может содержать произвольный URL gost- — на JID не влияет; считаются LOGTEXT и REPLYTO."""
    p = EgiszMonitorParser()
    xml = _soap("MSG-GOST", "success", kind="<ns2:kind>62</ns2:kind>")
    extra = "http://gost-77.infoclinica.lan/mentioned-in-body"
    msg = f"{extra}\n{xml}"
    log = "http://gost-12.infoclinica.lan/callback"
    rec = p.build_record(
        log,
        msg_text=msg,
        reply_to="http://gost-5.infoclinica.lan/",
        document_id="DOC-GOST-BODY",
        kind_from_egisz_licenses="62",
    )
    assert rec is not None
    assert rec.relates_to_id == "MSG-GOST"
    assert rec.jid == 12
    assert rec.gost_jid_token is None
    assert rec.jid_from_gost_log == 12
    assert rec.jid_from_gost_reply == 5
    assert rec.jid_sources_mismatch is True


def test_build_record_jid_mismatch_license_vs_gost_log() -> None:
    p = EgiszMonitorParser()
    xml = _soap("M-LIC", "success", kind="<ns2:kind>62</ns2:kind>")
    rec = p.build_record(
        "http://gost-10.infoclinica.lan/",
        msg_text=xml,
        document_id="DOC-LIC-MISMATCH",
        jid_from_egisz_licenses_row=99,
        kind_from_egisz_licenses="62",
    )
    assert rec is not None
    assert rec.jid == 10
    assert rec.jid_from_license == 99
    assert rec.jid_from_gost_log == 10
    assert rec.jid_sources_mismatch is True


def test_local_uid_from_xml_overrides_document_id() -> None:
    p = EgiszMonitorParser()
    inner = """
      <ns2:relatesToMessage>MSG-LU</ns2:relatesToMessage>
      <ns2:localUid>XML-UID-1</ns2:localUid>
      <ns2:status>success</ns2:status>
      <ns2:kind>62</ns2:kind>
    """
    xml = f"""<?xml version="1.0"?>
<soap:Envelope xmlns:soap="http://schemas.xmlsoap.org/soap/envelope/" xmlns:ns2="{NS}">
  <soap:Body><ns2:registerDocumentResult>{inner}</ns2:registerDocumentResult></soap:Body></soap:Envelope>"""
    rec = p.build_record("http://gost-1.infoclinica.lan/", msg_text=xml, document_id="DOC-OVERRIDE")
    assert rec is not None
    assert rec.local_uid_semd == "XML-UID-1"


def test_parse_xml_success_with_registry() -> None:
    p = EgiszMonitorParser()
    xml = _soap(
        "MSG-001",
        "success",
        success_block="""
      <ns2:registryItem>
        <ns2:emdrId>EMDR-9</ns2:emdrId>
        <ns2:registrationDate>2024-01-15T10:00:00Z</ns2:registrationDate>
      </ns2:registryItem>
        """,
    )
    out = p.parse_xml(xml)
    assert out is not None
    assert out["relates_to_id"] == "MSG-001"
    assert out["status"] == "success"
    assert out["kind_code"] == "62"
    assert out["org_oid"] == "1.2.643.5.1.13.13.99.99"
    assert out["emdr_id"] == "EMDR-9"
    assert out["registration_date"] is not None
    assert out["errors"] == []


def test_parse_xml_kind_with_nested_text() -> None:
    """KIND may be only under child elements (not el.text on <kind>)."""
    p = EgiszMonitorParser()
    inner = """
      <ns2:relatesToMessage>MSG-KNEST</ns2:relatesToMessage>
      <ns2:status>success</ns2:status>
      <ns2:kind><ns2:code>187</ns2:code></ns2:kind>
      <ns2:organization>1.2.643.5.1.13.13.99.99</ns2:organization>
    """
    xml = f"""<?xml version="1.0"?>
<soap:Envelope xmlns:soap="http://schemas.xmlsoap.org/soap/envelope/" xmlns:ns2="{NS}">
  <soap:Body><ns2:registerDocumentResult>{inner}</ns2:registerDocumentResult></soap:Body></soap:Envelope>"""
    out = p.parse_xml(xml)
    assert out is not None
    assert out["kind_code"] == "187"


def test_parse_xml_prefers_registration_date_time_over_registration_date() -> None:
    p = EgiszMonitorParser()
    inner = """
      <ns2:relatesToMessage>MSG-RDT</ns2:relatesToMessage>
      <ns2:status>success</ns2:status>
      <ns2:kind>62</ns2:kind>
      <ns2:registryItem>
        <ns2:registrationDate>2024-06-01T00:00:00Z</ns2:registrationDate>
        <ns2:registrationDateTime>2024-06-02T12:00:00Z</ns2:registrationDateTime>
      </ns2:registryItem>
    """
    xml = f"""<?xml version="1.0"?>
<soap:Envelope xmlns:soap="http://schemas.xmlsoap.org/soap/envelope/" xmlns:ns2="{NS}">
  <soap:Body><ns2:registerDocumentResult>{inner}</ns2:registerDocumentResult></soap:Body></soap:Envelope>"""
    out = p.parse_xml(xml)
    assert out is not None
    assert out["registration_date"] is not None
    assert out["registration_date"].year == 2024
    assert out["registration_date"].month == 6
    assert out["registration_date"].day == 2


def test_parse_xml_creation_date_time() -> None:
    p = EgiszMonitorParser()
    inner = """
      <ns2:relatesToMessage>MSG-CRE</ns2:relatesToMessage>
      <ns2:status>success</ns2:status>
      <ns2:kind>62</ns2:kind>
      <ns2:registryItem>
        <ns2:creationDateTime>2024-03-10T08:30:00Z</ns2:creationDateTime>
      </ns2:registryItem>
    """
    xml = f"""<?xml version="1.0"?>
<soap:Envelope xmlns:soap="http://schemas.xmlsoap.org/soap/envelope/" xmlns:ns2="{NS}">
  <soap:Body><ns2:registerDocumentResult>{inner}</ns2:registerDocumentResult></soap:Body></soap:Envelope>"""
    out = p.parse_xml(xml)
    assert out is not None
    assert out["semd_creation_at"] is not None
    assert out["semd_creation_at"].month == 3
    assert out["semd_creation_at"].day == 10


def test_parse_xml_errors_array() -> None:
    p = EgiszMonitorParser()
    err = """
      <ns2:errors>
        <ns2:item><ns2:code>E1</ns2:code><ns2:message>Bad</ns2:message></ns2:item>
        <ns2:item><ns2:code>E2</ns2:code><ns2:message>Worse</ns2:message></ns2:item>
      </ns2:errors>
    """
    xml = _soap("MSG-002", "error", kind=None, org=None, errors_block=err)
    out = p.parse_xml(xml)
    assert out is not None
    assert out["status"] == "error"
    assert len(out["errors"]) == 2
    assert out["errors"][0]["code"] == "E1"


def test_kind_fallback_from_egisz_licenses_kind_only() -> None:
    p = EgiszMonitorParser()
    xml = _soap("MSG-003", "success", kind=None, org=None, success_block="")
    log = "http://gost-7.infoclinica.lan/callback"
    rec = p.build_record(log, msg_text=xml, document_id="DOC-KIND-FB", kind_from_egisz_licenses="43")
    assert rec is not None
    assert rec.kind_code == "43"
    assert "Направление" in (rec.kind_name or "")


def test_resolve_jid_via_oid_map() -> None:
    p = EgiszMonitorParser()
    xml = _soap("MSG-004", "success", kind=None, org="<ns2:organization>OID-X</ns2:organization>")
    mo_uid_to_jid = {"OID-X": 999}
    rec = p.build_record(
        "",
        msg_text=xml,
        document_id="DOC-OID-MAP",
        jid_by_mo_uid_from_egisz_licenses=mo_uid_to_jid,
    )
    assert rec is not None
    assert rec.jid == 999
    assert rec.org_oid == "OID-X"


def test_extract_parse_hints_from_raw_xml_fragments() -> None:
    raw = (
        "<ns2:relatesToMessage xmlns:ns2=\"http://egisz.rosminzdrav.ru/iehr/emdr/callback/\">R-HINT</ns2:relatesToMessage>"
        "<ns2:localUid>L-HINT</ns2:localUid><ns2:emdrId>E-HINT</ns2:emdrId>"
    )
    r, l, e = extract_parse_hints(raw)
    assert r == "R-HINT"
    assert l == "L-HINT"
    assert e == "E-HINT"


def test_xml_broken_staging_stores_hints_when_regex_matches() -> None:
    """parse_xml returns None but relatesToMessage appears in text → XML_BROKEN + hints."""
    bad = (
        "<ns2:relatesToMessage xmlns:ns2=\"http://egisz.rosminzdrav.ru/iehr/emdr/callback/\">MSG-Z</ns2:relatesToMessage>"
        "<ns2:localUid>LU-Z</ns2:localUid>"
        "<<<not-xml>>>"
    )
    assert extract_parse_hints(bad)[0] == "MSG-Z"
    assert extract_parse_hints(bad)[1] == "LU-Z"

    p = EgiszMonitorParser()
    errors: list = []

    def on_err(e) -> None:
        errors.append(e)

    rec = p.build_record(
        "http://gost-1.infoclinica.lan/",
        msg_text=bad,
        on_staging_error=on_err,
        exchangelog_log_id=9001,
        egisz_messages_egmid=8002,
        journal_msgid="MID-1",
    )
    assert rec is None
    assert len(errors) == 1
    assert errors[0].error_code == "XML_BROKEN"
    assert errors[0].relates_to_hint == "MSG-Z"
    assert errors[0].local_uid_hint == "LU-Z"
    assert errors[0].exchangelog_log_id == 9001
    assert errors[0].journal_msgid == "MID-1"


def test_staging_error_missing_relates() -> None:
    p = EgiszMonitorParser()
    errors: list = []

    def on_err(e) -> None:
        errors.append(e)

    bad = "<ns2:registerDocumentResult xmlns:ns2='%s'><ns2:status>success</ns2:status></ns2:registerDocumentResult>" % NS
    rec = p.build_record("", msg_text=bad, on_staging_error=on_err)
    assert rec is None
    assert len(errors) == 1
    assert errors[0].error_code == "MISSING_RELATES_TO"


def test_staging_error_when_relates_but_no_localuid_or_emdr() -> None:
    """relatesToMessage без localUid, без DOCUMENTID и без emdrId — только stg_channel_errors, не факт."""
    p = EgiszMonitorParser()
    errors: list = []

    def on_err(e) -> None:
        errors.append(e)

    xml = _soap("MSG-NO-DOC-KEYS", "success", kind="<ns2:kind>62</ns2:kind>")
    rec = p.build_record(
        "http://gost-1.infoclinica.lan/",
        msg_text=xml,
        document_id=None,
        on_staging_error=on_err,
        exchangelog_log_id=12_345,
    )
    assert rec is None
    assert len(errors) == 1
    assert errors[0].error_code == "MISSING_DOCUMENT_IDENTIFIERS"
    assert errors[0].relates_to_id == "MSG-NO-DOC-KEYS"
    assert errors[0].exchangelog_log_id == 12_345


def test_as_fact_row_errors_list() -> None:
    p = EgiszMonitorParser()
    xml = _soap("ID-5", "error", kind="<ns2:kind>001</ns2:kind>", errors_block="""
      <ns2:errors><ns2:item><ns2:code>C</ns2:code><ns2:message>M</ns2:message></ns2:item></ns2:errors>
    """)
    rec = p.build_record("", msg_text=xml, document_id="DOC-ERR-LIST")
    assert rec is not None
    row = rec.as_fact_row()
    assert row["errors_json"][0]["code"] == "C"
    assert "local_uid_semd" in row


def test_build_record_passes_exchangelog_log_id_and_message_egmid() -> None:
    p = EgiszMonitorParser()
    xml = _soap("SRC-1", "success", kind="<ns2:kind>62</ns2:kind>")
    rec = p.build_record(
        "http://gost-1.infoclinica.lan/",
        msg_text=xml,
        document_id="D1",
        exchangelog_log_id=991_001,
        egisz_messages_egmid=42,
    )
    assert rec is not None
    row = rec.as_fact_row()
    assert row["exchangelog_log_id"] == 991_001
    assert row["egisz_messages_egmid"] == 42


def test_build_record_stores_journal_msgid() -> None:
    p = EgiszMonitorParser()
    xml = _soap("SRC-2", "success", kind="<ns2:kind>62</ns2:kind>")
    rec = p.build_record(
        "http://gost-1.infoclinica.lan/",
        msg_text=xml,
        document_id="D1",
        journal_msgid="92F20A635181473996FD142B0603CA04",
    )
    assert rec is not None
    assert rec.as_fact_row()["journal_msgid"] == "92F20A635181473996FD142B0603CA04"


def test_parse_xml_relates_from_attribute_when_element_empty() -> None:
    p = EgiszMonitorParser()
    xml = f"""<?xml version="1.0"?>
<soap:Envelope xmlns:soap="http://schemas.xmlsoap.org/soap/envelope/" xmlns:ns2="{NS}">
  <soap:Body>
    <ns2:registerDocumentResult relatesToMessage="ATTR-REL-1">
      <ns2:status>success</ns2:status>
      <ns2:kind>62</ns2:kind>
    </ns2:registerDocumentResult>
  </soap:Body>
</soap:Envelope>"""
    out = p.parse_xml(xml)
    assert out is not None
    assert out.get("relates_to_id") == "ATTR-REL-1"


def test_parse_xml_opens_without_registerdocumentresult_literal_when_relates_present() -> None:
    """Поздние/укороченные ответы: без подстроки registerDocumentResult, но с тегом relatesToMessage."""
    p = EgiszMonitorParser()
    xml = f"""<?xml version="1.0"?>
<Outer xmlns:ns2="{NS}">
  <ns2:relatesToMessage>LATE-MSG-1</ns2:relatesToMessage>
  <ns2:status>success</ns2:status>
  <ns2:kind>62</ns2:kind>
</Outer>"""
    out = p.parse_xml(xml)
    assert out is not None
    assert out.get("relates_to_id") == "LATE-MSG-1"


def test_parse_xml_rejects_doctype_with_internal_entity() -> None:
    """defusedxml forbids DTD/entity expansion (Gordon-2 baseline)."""
    p = EgiszMonitorParser()
    xml = """<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE registerDocumentResult [
  <!ENTITY xxe "INJECT">
]>
<registerDocumentResult xmlns="http://egisz.rosminzdrav.ru/iehr/emdr/callback/">
  <relatesToMessage>&xxe;</relatesToMessage>
  <status>success</status>
</registerDocumentResult>"""
    assert p.parse_xml(xml) is None


def test_parse_xml_rejects_external_entity_system() -> None:
    p = EgiszMonitorParser()
    xml = """<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE registerDocumentResult [
  <!ENTITY xxe SYSTEM "http://127.0.0.1:9/does-not-exist">
]>
<registerDocumentResult xmlns="http://egisz.rosminzdrav.ru/iehr/emdr/callback/">
  <relatesToMessage>&xxe;</relatesToMessage>
  <status>success</status>
</registerDocumentResult>"""
    assert p.parse_xml(xml) is None


def test_coerce_exchangelog_log_state_firebird_shapes() -> None:
    assert coerce_exchangelog_log_state(None) is None
    assert coerce_exchangelog_log_state(3) == 3
    assert coerce_exchangelog_log_state("3") == 3
    assert coerce_exchangelog_log_state(b"3") == 3
    assert coerce_exchangelog_log_state(Decimal("3")) == 3
    assert coerce_exchangelog_log_state(3.0) == 3
    assert coerce_exchangelog_log_state(3.7) is None
    assert coerce_exchangelog_log_state(True) is None


def test_build_record_logstate_3_network_error_typed_like_firebird() -> None:
    p = EgiszMonitorParser()
    staged: list = []

    def on_staging_error(e) -> None:
        staged.append(e)

    for raw_ls in ("3", b"3", Decimal("3")):
        staged.clear()
        r = p.build_record(
            "http://gost-5008.infoclinica.lan:9945/timeout",
            msg_text=None,
            log_state=raw_ls,
            on_staging_error=on_staging_error,
            journal_msgid="MSG-NET",
        )
        assert r is None
        assert len(staged) == 1
        assert staged[0].error_code == "INTEGRATION_LOGSTATE_3"
        assert staged[0].error_top_type == "network"
        assert staged[0].error_group == "network"
        assert staged[0].error_subtype == "logstate_3"
