# Gordon-2: XML безопасность и корректность (копировать в Gordon)

Скопируйте блок ниже в Docker Gordon (Files). Ответ сохраните в issue/PR; изменения в код вносите отдельным PR после ревью.

```text
Репозиторий: egisz-monitor-corp. Фокус: egisz_monitor_corp/parser.py — парсинг MSGTEXT (SOAP) после перехода на defusedxml.ElementTree.fromstring (запрет опасных DTD/сущностей).

Оцени оставшиеся риски (квадратичные деревья, DoS по размеру документа) для типичного SOAP EGISZ. Сравни с lxml resolve_entities=False и лимитами глубины/размера. Предложи минимальные доработки (1 страница) + golden-тесты из анонимизированных прод-фрагментов.

Без автоматического git commit.
```

## Уже в репо (контекст)

- Парсинг XML из MSGTEXT: **`defusedxml.ElementTree.fromstring`** в [`egisz_monitor_corp/parser.py`](../egisz_monitor_corp/parser.py) (зависимость `defusedxml` в [`pyproject.toml`](../pyproject.toml)); `xml.etree.ElementTree` остаётся только для типа `ParseError` и аннотаций элементов.
- Лимит размера MSGTEXT: `etl.max_msgtext_bytes` в YAML, staging `MSGTEXT_TOO_LARGE` в [`egisz_monitor_corp/etl.py`](../egisz_monitor_corp/etl.py).
- Таймауты Firebird SELECT: [`egisz_monitor_corp/fb_client.py`](../egisz_monitor_corp/fb_client.py) `fetch_all(..., timeout_sec=...)`.

## После ответа Gordon

1. Согласовать оставшиеся меры (лимиты глубины/размера дерева, lxml и т.д.) с командой.
2. Отдельный PR при необходимости: доработки `parser.py` + тесты.
