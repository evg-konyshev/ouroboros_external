from __future__ import annotations

import pytest

from finengine.assistant import search_tool
from finengine.assistant.ouro_view import search_hits
from finengine.assistant.turn_record import TurnRecord
from finengine.search.search import SearchHit, _fused_chunk_owners


def test_search_keeps_short_hits_after_an_oversized_table() -> None:
    """C3.11 — длинная таблица не скрывает следующий релевантный фрагмент."""
    long = SearchHit("doc-long", "1", 3, "Таблицы", "А" * 200, 1.0)
    short = SearchHit("doc-short", "2", 4, "Риски", "Краткий фрагмент", 0.5)

    result = search_hits([long, short], degraded=False, limit=170)

    assert "doc-short | стр. 4" in result
    assert "ответ обрезан" in result
    assert len(result) <= 170


def test_search_returns_a_cited_excerpt_when_only_the_long_hit_matches() -> None:
    hit = SearchHit("doc-long", "1", 3, "Таблицы", "А" * 200, 1.0)

    result = search_hits([hit], degraded=False, limit=150)

    assert "doc-long | стр. 3" in result
    assert "ААА" in result
    assert "ответ обрезан" in result
    assert len(result) <= 150


def test_search_counts_the_truncation_marker_in_the_result_limit() -> None:
    first = SearchHit("doc", "1", 1, "", "А" * 100, 1.0)
    second = SearchHit("doc", "2", 2, "", "Б" * 100, 0.5)
    limit = len(search_hits([first], degraded=False, limit=1_000))

    assert len(search_hits([first, second], degraded=False, limit=limit)) <= limit


@pytest.mark.parametrize(("role", "is_admin"), [("admin", False), ("analyst", True)])
def test_search_uses_privileges_from_the_authenticated_turn(
    monkeypatch: pytest.MonkeyPatch,
    role: str,
    is_admin: bool,
) -> None:
    monkeypatch.setattr(search_tool, "get_store", lambda: None)
    record = TurnRecord("run", "7", "chat", "message", 7, "case", (), "", role, is_admin=is_admin)

    search = search_tool._default_search(record)

    assert search.user.is_admin is is_admin


def test_search_excerpt_has_evidence_even_when_the_section_title_is_too_long() -> None:
    hit = SearchHit("doc", "1", 0, "Раздел " * 80, "Содержимое документа.", 1.0)

    result = search_hits([hit], degraded=False, limit=160)

    assert "страница не определена" in result
    assert "Содержимое документа." in result
    assert len(result) <= 160


def test_chunk_owners_are_deduplicated_after_the_chunk_rankings_are_fused() -> None:
    lexical = [
        {"_source": {"chunk_id": "a:1", "document_id": "a"}},
        {"_source": {"chunk_id": "b:1", "document_id": "b"}},
    ]
    vector = [
        {"_source": {"chunk_id": "a:2", "document_id": "a"}},
        {"_source": {"chunk_id": "b:1", "document_id": "b"}},
    ]

    assert _fused_chunk_owners(lexical, vector) == ["b", "a"]
