from __future__ import annotations

import asyncio
import threading
from dataclasses import dataclass, field

import pytest
from loop_support import Gateway, open_cycle
from tool_harness import Plugin, ctx_of, first_line, open_run

from finengine.assistant.tool_api import ToolApi
from finengine.search.client import SearchUnavailable
from finengine.search.embeddings import EmbeddingUnavailable
from finengine.search.search import SearchHit, SearchResult
from finengine.skills.spec.dynamic_run import ScenarioResult


class FakeSearch:
    def __init__(self) -> None:
        self.calls: list[dict] = []
        self.error: Exception | None = None
        self.degraded = False

    def search(self, allowed_ids, query, doc_ids=None, limit=None, mode="hybrid") -> SearchResult:
        self.calls.append(
            {
                "allowed_ids": allowed_ids,
                "query": query,
                "doc_ids": doc_ids,
                "limit": limit,
                "mode": mode,
            }
        )
        if self.error:
            raise self.error
        selected = doc_ids or ["selected"]
        return SearchResult(
            [
                SearchHit(
                    document_id=selected[0],
                    chunk_id="chunk-1",
                    page=3,
                    section_path="Риски",
                    content="Риск ликвидности",
                    score=1.0,
                    degraded=self.degraded,
                )
            ],
            degraded=self.degraded,
        )


class _NoGoal:
    async def __call__(self, record):
        raise AssertionError("aifa_open в этих тестах не вызывается")


async def _cycle_with_search(base, fake: FakeSearch, **kwargs):
    cycle = await open_cycle(base, **kwargs)
    await cycle.plugin.close()
    cycle.plugin = Plugin(
        ToolApi(Gateway(), _NoGoal(), cycle.desk, search_factory=lambda record: fake)
    )
    return cycle


@dataclass
class Held:
    started: threading.Event = field(default_factory=threading.Event)
    release: threading.Event = field(default_factory=threading.Event)

    def __call__(self, text: str) -> ScenarioResult:
        self.started.set()
        self.release.wait(10)
        return ScenarioResult(True, "готово")


async def test_foreign_doc_id_is_refused(base) -> None:
    cycle = await _cycle_with_search(base, FakeSearch(), doc_ids=("doc-1",))
    try:
        reply = await cycle.call("aifa_search", {"query": "риски", "doc_ids": ["foreign"]})
        assert first_line_of(reply) == "ОТКАЗ нет_документа"
    finally:
        await cycle.close()


async def test_search_before_open_has_the_same_refusal_as_doc(base) -> None:
    run = open_run(doc_ids=("doc-1",))
    plugin = Plugin(ToolApi(Gateway(), _NoGoal(), search_factory=lambda record: FakeSearch()))
    try:
        search = await plugin.call("aifa_search", ctx_of(run.run_id), {"query": "риски"})
        doc = await plugin.call("aifa_doc", ctx_of(run.run_id))
        assert first_line(search) == first_line(doc) == "ОТКАЗ нет_цикла"
    finally:
        await plugin.close()
        await run.end()


@pytest.mark.parametrize("limit", [0, 11, True, "2"])
async def test_limit_outside_one_to_ten_is_refused(base, limit) -> None:
    cycle = await _cycle_with_search(base, FakeSearch(), doc_ids=("doc-1",))
    try:
        reply = await cycle.call("aifa_search", {"query": "риски", "limit": limit})
        assert first_line_of(reply) == "ОТКАЗ неверные_аргументы"
    finally:
        await cycle.close()


@pytest.mark.parametrize("mode", [[], {}])
async def test_nonstring_mode_is_refused(base, mode) -> None:
    cycle = await _cycle_with_search(base, FakeSearch())
    try:
        reply = await cycle.call("aifa_search", {"query": "риски", "mode": mode})
        assert first_line_of(reply) == "ОТКАЗ неверные_аргументы"
    finally:
        await cycle.close()


async def test_without_doc_ids_the_pipeline_selects_documents_first(base) -> None:
    fake = FakeSearch()
    cycle = await _cycle_with_search(base, fake, doc_ids=("doc-1", "doc-2"))
    try:
        reply = await cycle.call("aifa_search", {"query": "риски"})
        assert fake.calls[0]["allowed_ids"] == ["doc-1", "doc-2"]
        assert fake.calls[0]["doc_ids"] is None
        assert fake.calls[0]["mode"] == "hybrid"
        assert "selected" in reply and "doc-1" not in reply
        assert first_line_of(reply) == "ГОТОВО"
    finally:
        await cycle.close()


async def test_explicit_doc_ids_and_lexical_mode_reach_the_service(base) -> None:
    fake = FakeSearch()
    cycle = await _cycle_with_search(base, fake, doc_ids=("doc-1", "doc-2"))
    try:
        reply = await cycle.call(
            "aifa_search", {"query": "прибыль", "doc_ids": ["doc-2"], "mode": "lexical"}
        )
        assert fake.calls[0]["doc_ids"] == ["doc-2"]
        assert fake.calls[0]["mode"] == "lexical"
        assert "doc-2" in reply
    finally:
        await cycle.close()


async def test_degraded_answer_is_marked_and_is_not_a_refusal(base) -> None:
    fake = FakeSearch()
    fake.degraded = True
    cycle = await _cycle_with_search(base, fake, doc_ids=("doc-1",))
    try:
        reply = await cycle.call("aifa_search", {"query": "ликвидности"})
        assert first_line_of(reply) == "ГОТОВО"
        assert "деградированный поиск" in reply
    finally:
        await cycle.close()


async def test_embedding_outage_refuses_with_lexical_alternative_and_no_retry(base) -> None:
    fake = FakeSearch()
    fake.error = EmbeddingUnavailable("провайдер недоступен")
    cycle = await _cycle_with_search(base, fake, doc_ids=("doc-1",))
    try:
        reply = await cycle.call("aifa_search", {"query": "риски"})
        assert first_line_of(reply) == "ОТКАЗ поиск_недоступен"
        assert 'mode="lexical"' in reply
        assert "повтори" not in reply
    finally:
        await cycle.close()


async def test_index_and_texts_outage_is_a_search_refusal(base) -> None:
    fake = FakeSearch()
    fake.error = SearchUnavailable("индекс и тексты недоступны")
    cycle = await _cycle_with_search(base, fake, doc_ids=("doc-1",))
    try:
        reply = await cycle.call("aifa_search", {"query": "риски"})
        assert first_line_of(reply) == "ОТКАЗ поиск_недоступен"
        assert "повтори" not in reply
    finally:
        await cycle.close()


async def test_search_answers_while_a_move_holds_the_queue(base) -> None:
    held = Held()
    cycle = await _cycle_with_search(
        base,
        FakeSearch(),
        doc_ids=("doc-1",),
        scenario=held,
    )
    try:
        running = asyncio.create_task(
            cycle.move('{"objective": "ход", "reason": "нужно", "scenarios": ["долгий"]}')
        )
        await asyncio.to_thread(held.started.wait, 5)
        reply = await asyncio.wait_for(cycle.call("aifa_search", {"query": "риски"}), timeout=2)
        held.release.set()
        assert first_line_of(reply) == "ГОТОВО"
        assert first_line_of(await running) == "ГОТОВО"
    finally:
        await cycle.close()


def first_line_of(reply: str) -> str:
    return reply.split("\n", 1)[0]
