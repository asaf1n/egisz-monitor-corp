"""Классификация staging-ошибок канала ETL (два глобальных типа)."""

from __future__ import annotations

from egisz_monitor_corp.error_model import (
    ERROR_TOP_TYPE_LABEL_RU,
    ErrorTopType,
    classify_staging_error_code,
)


def test_integration_codes_map_to_connectivity_top_type() -> None:
    for code in ("INTEGRATION_LOGSTATE_3", "NETWORK_ERROR"):
        c = classify_staging_error_code(code)
        assert c.top_type == ErrorTopType.NETWORK
        assert c.group.value == "network"
        assert c.subtype == "logstate_3"


def test_async_response_subtypes() -> None:
    assert classify_staging_error_code("XML_BROKEN").top_type == ErrorTopType.ASYNC_RESPONSE
    assert classify_staging_error_code("MISSING_RELATES_TO").group.value == "linkage"


def test_top_type_ru_labels() -> None:
    assert ERROR_TOP_TYPE_LABEL_RU["network"] == "Ошибка связи"
    assert ERROR_TOP_TYPE_LABEL_RU["async_response"] == "Ошибка в асинхронном ответе РЭМД"
