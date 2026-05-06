"""Справочник СЭМД: разные коды НСИ с похожей линейкой — разные наименования (редакции)."""

from __future__ import annotations

from egisz_monitor_corp.semd_dictionary import get_semd_name


def test_consultation_protocol_119_and_227_are_distinct_editions() -> None:
    """119 и 227 — разные документы ЕГИСЗ (разные редакции по НСИ 1.2.643.5.1.13.13.11.1520)."""
    n119 = get_semd_name("119")
    n227 = get_semd_name("227")
    assert "Редакция 4" in n119
    assert "Редакция 5" in n227
    assert n119 != n227

