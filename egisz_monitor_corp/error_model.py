from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class ErrorTopType(str, Enum):
    """Два глобальных типа для мониторинга (значения в БД — machine-readable)."""

    # Ошибка связи / транспорта / интеграционного контура (не разбор SOAP колбэка РЭМД).
    NETWORK = "network"
    # Ошибка в асинхронном ответе РЭМД: разбор колбэка, связка, идентификаторы.
    ASYNC_RESPONSE = "async_response"


# Подписи верхнего уровня для отчётов и UI (согласовано с v_stg_channel_errors_by_document.error_global_subcategory).
ERROR_TOP_TYPE_LABEL_RU: dict[str, str] = {
    ErrorTopType.NETWORK.value: "Ошибка связи",
    ErrorTopType.ASYNC_RESPONSE.value: "Ошибка в асинхронном ответе РЭМД",
}


class StagingErrorGroup(str, Enum):
    """Внутренние группы внутри глобального типа (таблица stg_channel_errors)."""

    NETWORK = "network"
    PARSE = "parse"
    LINKAGE = "linkage"
    IDENTIFIERS = "identifiers"
    OTHER = "other"


# Коды ошибок: сбой связи/журнала (LOGSTATE=3 и др.), не путать с XML_BROKEN и т.п.
_INTEGRATION_CONNECTIVITY_CODES = frozenset({"INTEGRATION_LOGSTATE_3", "NETWORK_ERROR"})


@dataclass(frozen=True)
class StagingErrorClass:
    top_type: ErrorTopType
    group: StagingErrorGroup
    subtype: str


def classify_staging_error_code(error_code: str) -> StagingErrorClass:
    """
    Классификация записей staging-ошибок канала ETL (таблица ``stg_channel_errors``).

    Два глобальных типа: **связь/интеграция** (``network``) и **асинхронный ответ РЭМД** (``async_response``).
    ``NETWORK_ERROR`` — устаревший код, эквивалентен ``INTEGRATION_LOGSTATE_3``.
    """
    code = (error_code or "").strip().upper()
    if code in _INTEGRATION_CONNECTIVITY_CODES:
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

