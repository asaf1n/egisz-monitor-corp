"""Справочник типов СЭМД: идентификатор и наименование по НСИ для ETL и отчётов.

Канонический источник для обновления данных — НСИ «Регистрируемые электронные
медицинские документы», OID ``1.2.643.5.1.13.13.11.1520``; паспорт справочника
(поля «Идентификатор», «Наименование» и др.):
https://nsi.rosminzdrav.ru/dictionaries/1.2.643.5.1.13.13.11.1520/passport/12.33

Офлайн-кэш в репозитории: ``nsi1520_kind_names.json``; вспомогательно —
``data/nsi1520_confluence_export.txt``.

Коды из фактов, отсутствующие в кэше, — в ``nsi1520_kind_names_supplemental.json``
(при пересечении приоритет у дополнения).

Разные коды НСИ — разные документы для ЕГИСЗ (в т.ч. разные редакции, напр. 119 vs 227).
"""

from __future__ import annotations

import json
from pathlib import Path


def _load_kind_table(name: str) -> dict[str, str]:
    path = Path(__file__).with_name(name)
    if not path.is_file():
        return {}
    data = json.loads(path.read_text(encoding="utf-8"))
    return {str(k): str(v) for k, v in data.items()}


SEMD_DICTIONARY: dict[str, str] = {
    **_load_kind_table("nsi1520_kind_names.json"),
    **_load_kind_table("nsi1520_kind_names_supplemental.json"),
}


def get_semd_name(code: str | int | None) -> str:
    if code is None or code == "":
        return "Тип СЭМД не указан"
    key = str(code).strip()
    return SEMD_DICTIONARY.get(key) or f"{key} — наименование СЭМД отсутствует в справочнике"
