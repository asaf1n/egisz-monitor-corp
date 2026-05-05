from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class ErrorTopType(str, Enum):
    """Два больших типа ошибок для мониторинга витрины."""

    NETWORK = "network"
    ASYNC_RESPONSE = "async_response"


class StagingErrorGroup(str, Enum):
    """Внутренние группировки ошибок парсинга (stg_parse_errors)."""

    NETWORK = "network"
    PARSE = "parse"
    LINKAGE = "linkage"
    IDENTIFIERS = "identifiers"
    OTHER = "other"


@dataclass(frozen=True)
class StagingErrorClass:
    top_type: ErrorTopType
    group: StagingErrorGroup
    subtype: str


def classify_staging_error_code(error_code: str) -> StagingErrorClass:
    """
    Классификация ошибок ETL-парсинга (stg_parse_errors).

    Требование прототипа: стабильный top-level (network vs async_response) + внутренние группы.
    """
    code = (error_code or "").strip().upper()
    if code == "NETWORK_ERROR":
        return StagingErrorClass(
            top_type=ErrorTopType.NETWORK,
            group=StagingErrorGroup.NETWORK,
            subtype="logstate_3",
        )

    # Всё остальное — проблемы обработки/разбора асинхронного колбэка (SOAP есть/ожидается, но факт не построен).
    if code in {"XML_BROKEN", "MSGTEXT_TOO_LARGE"}:
        return StagingErrorClass(
            top_type=ErrorTopType.ASYNC_RESPONSE,
            group=StagingErrorGroup.PARSE,
            subtype=code.lower(),
        )
    if code == "MISSING_RELATES_TO":
        return StagingErrorClass(
            top_type=ErrorTopType.ASYNC_RESPONSE,
            group=StagingErrorGroup.LINKAGE,
            subtype="missing_relates_to_message",
        )
    if code == "MISSING_DOCUMENT_IDENTIFIERS":
        return StagingErrorClass(
            top_type=ErrorTopType.ASYNC_RESPONSE,
            group=StagingErrorGroup.IDENTIFIERS,
            subtype="missing_localuid_documentid_emdrid",
        )

    return StagingErrorClass(
        top_type=ErrorTopType.ASYNC_RESPONSE,
        group=StagingErrorGroup.OTHER,
        subtype=code.lower() or "unknown",
    )

