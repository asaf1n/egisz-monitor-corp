"""Tests for EgiszMonitorParser."""

from __future__ import annotations

from egisz_monitor_corp.parser import EgiszMonitorParser


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
    assert r["jid"] == 42
    assert r["gost_jid_token"] == "42"


def test_extract_jid_alphanumeric_token_no_int_jid() -> None:
    p = EgiszMonitorParser()
    log = "http://gost-clinic-a.infoclinica.lan:443/"
    r = p.extract_jid(log)
    assert r["jid"] is None
    assert r["gost_jid_token"] == "clinic-a"


def test_extract_jid_falls_back_to_reply_to_when_log_has_no_gost() -> None:
    p = EgiszMonitorParser()
    log = "no gost host here"
    reply = "https://gost-99.infoclinica.lan/soap"
    r = p.extract_jid(log, reply_to=reply)
    assert r["jid"] == 99
    assert r["gost_jid_token"] == "99"


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
    assert rec.gost_jid_token == "5"


def test_soap_not_parsed_from_logtext_xml_must_be_msgtext() -> None:
    """LOGTEXT never carries SOAP; XML only in MSGTEXT."""
    p = EgiszMonitorParser()
    xml = _soap("MSG-ONLY-MSG", "success", kind="<ns2:kind>62</ns2:kind>")
    log = "http://gost-3.infoclinica.lan/"
    rec = p.build_record(log, msg_text=None, document_id="x")
    assert rec is None
    rec2 = p.build_record(log, msg_text=xml)
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
    rec = p.build_record(log, msg_text=xml, kind_from_egisz_licenses="43")
    assert rec is not None
    assert rec.kind_code == "43"
    assert "Направление" in (rec.kind_name or "")


def test_resolve_jid_via_oid_map() -> None:
    p = EgiszMonitorParser()
    xml = _soap("MSG-004", "success", kind=None, org="<ns2:organization>OID-X</ns2:organization>")
    mo_uid_to_jid = {"OID-X": 999}
    rec = p.build_record("", msg_text=xml, jid_by_mo_uid_from_egisz_licenses=mo_uid_to_jid)
    assert rec is not None
    assert rec.jid == 999
    assert rec.org_oid == "OID-X"


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


def test_as_fact_row_errors_list() -> None:
    p = EgiszMonitorParser()
    xml = _soap("ID-5", "error", kind="<ns2:kind>001</ns2:kind>", errors_block="""
      <ns2:errors><ns2:item><ns2:code>C</ns2:code><ns2:message>M</ns2:message></ns2:item></ns2:errors>
    """)
    rec = p.build_record("", msg_text=xml)
    assert rec is not None
    row = rec.as_fact_row()
    assert row["errors_json"][0]["code"] == "C"
    assert "local_uid_semd" in row
