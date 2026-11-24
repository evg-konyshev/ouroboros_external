"""Чтение цикла: `aifa_state` (состояние и артефакты) и `aifa_doc` (текст документов хода), C3.6, ADR-0040 п. 2.

Читают только Case и документы этого хода; ответ не длиннее `result_limit`, остаток — по просьбе.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest
from loop_support import Gateway, make_passport, open_cycle
from test_dynamic_run import ExtractScenario, _metric_for_period, _move
from test_ephemeral_in_graph import CASE, OWNER
from tool_harness import Plugin, ctx_of, open_run

from finengine.assistant import read_tools
from finengine.assistant.tool_api import ToolApi
from finengine.contracts import DynamicArtifact, NodeKey
from finengine.models.document import DocumentMeta
from finengine.search.client import SearchUnavailable
from finengine.search.search import DocumentScore

LIMIT = 12_000
DOC = "doc-1"
PAGES = {
    1: "Титульный лист отчёта ЛУКОЙЛ.",
    2: "Выручка за 2024 год составила 8 000 млрд руб.",
    3: "Приложение: EBITDA за 2024 год — 1 200 млрд руб.",
}
TEXT = "".join(f"===PAGE {number}===\n{text}\n" for number, text in PAGES.items())


def code_of(reply: str) -> str:
    return reply.split("\n", 1)[0]


@pytest.fixture
def documents(monkeypatch: pytest.MonkeyPatch) -> None:
    texts = {DOC: TEXT, "doc-long": "".join(f"===PAGE {n}===\n{'слово ' * 700}\n" for n in range(1, 6))}
    metas = {
        doc_id: DocumentMeta(doc_id=doc_id, filename=f"{doc_id}.pdf", company_name="ЛУКОЙЛ")
        for doc_id in texts
    }
    monkeypatch.setattr(read_tools, "document_text", lambda doc_id, **_: texts.get(doc_id))
    monkeypatch.setattr(read_tools, "document_text_exists", texts.__contains__)
    monkeypatch.setattr(
        read_tools, "get_store", lambda: SimpleNamespace(
            get_document=metas.get,
            set_text_pages=lambda doc_id, count, **_: setattr(metas[doc_id], "text_pages", count),
        )
    )


def _put_dynamic(base, name: str, payload: dict, owner: str = OWNER) -> str:
    artifact = DynamicArtifact(
        output_type=f"dyn:{name}", payload=payload, origin=["generated"], producer=f"eph.{name}@1",
        input_evidence_ids=[],
    )
    base.ensure_case(owner_id=owner, case_id=CASE)
    base.store.put_artifact(
        owner, CASE, node_key=NodeKey(atom_name=f"eph.{name}", atom_version="1", args_fingerprint=name),
        output_type=artifact.output_type, payloads=(artifact,), concept=None, entity_ref=None, period_ref=None,
        basis=None, document_version=None, policy_version="test",
    )
    return artifact.evidence_id


async def test_the_state_shows_the_loop_the_way_the_planner_sees_it(base) -> None:
    """§3.1: состояние цикла — вопрос, цель, план, каталог, артефакты со значениями, журнал, остаток."""
    cycle = await open_cycle(base, scenario=ExtractScenario(base, None))
    await cycle.move(_move(scenarios=("Извлеки EBITDA и выручку",)))

    state = await cycle.call("aifa_state")

    assert code_of(state) == "ГОТОВО"
    for section in ("Вопрос пользователя", "Разобранная цель", "Каталог атомов", "Журнал ходов", "Осталось"):
        assert f"## {section}" in state
    assert "EBITDA" in state and "500.0" in state, "значения артефактов — в таблице"
    assert len(state) <= LIMIT
    await cycle.close()


async def test_the_frozen_registry_is_readable_through_the_state_http_tool(base) -> None:
    """ADR-0096: отдельный режим HTTP-инструмента возвращает формулы реестра без усечения."""
    cycle = await open_cycle(base, passport=make_passport())
    try:
        reply = await cycle.call("aifa_state", {"registry_offset": 0})
        assert code_of(reply) == "ГОТОВО" and len(reply) <= LIMIT
        assert "Рассчитываемые" in reply
        assert "OPERATING_PROFIT + DEPRECIATION_AMORTIZATION" in reply
        assert "NET_DEBT" in reply and "BANK_DEPOSITS" in reply
        assert reply.endswith("next_offset=null")
    finally:
        await cycle.close()


@pytest.mark.parametrize("offset", [-1, True, "0", None])
async def test_registry_offsets_are_nonnegative_integers(base, offset) -> None:
    cycle = await open_cycle(base)
    try:
        reply = await cycle.call("aifa_state", {"registry_offset": offset})
        assert code_of(reply) == "ОТКАЗ неверные_аргументы"
    finally:
        await cycle.close()


@pytest.mark.parametrize("selector", [{"artifact_ids": []}, {"artifact_index": {}}])
async def test_the_registry_cannot_be_requested_together_with_artifacts(base, selector) -> None:
    cycle = await open_cycle(base)
    try:
        reply = await cycle.call("aifa_state", {"registry_offset": 0, **selector})
        assert code_of(reply) == "ОТКАЗ неверные_аргументы"
    finally:
        await cycle.close()


async def test_a_state_that_does_not_fit_gives_the_table_the_room_and_names_the_rest(base) -> None:
    """§3.0: длинное — частями; остаток таблицы читается `aifa_state(artifact_ids)`."""
    for index in range(80):
        _put_dynamic(base, f"row{index}", {"text": "данные " * 40, "index": index})
    cycle = await open_cycle(base)

    state = await cycle.call("aifa_state")

    assert code_of(state) == "ГОТОВО" and len(state) <= LIMIT
    assert "Остальные — aifa_state с artifact_ids" in state
    await cycle.close()


async def test_artifacts_by_id_come_whole(base) -> None:
    """§3.1: `aifa_state(artifact_ids)` — полное содержимое: значения, единицы, периоды, источник."""
    cycle = await open_cycle(base, scenario=ExtractScenario(base, None))
    await cycle.move(_move(scenarios=("Извлеки EBITDA и выручку",)))
    metric = next(
        payload for row in base.store.list_artifacts(OWNER, CASE) for payload in row["payloads"]
        if getattr(payload, "concept", None) == "EBITDA"
    )

    reply = await cycle.call("aifa_state", {"artifact_ids": [metric.scalar_fact.evidence_id]})

    assert code_of(reply) == "ГОТОВО" and "### " + metric.scalar_fact.evidence_id in reply
    assert '"value":500.0' in reply.replace(" ", "") and "RUB" in reply.upper() or "unit" in reply
    await cycle.close()


async def test_only_the_case_of_this_turn_is_readable(base) -> None:
    """LR-35, ADR-0040 п. 2: артефакт другого владельца по id не читается, как и несуществующий."""
    foreign = _put_dynamic(base, "foreign", {"secret": "чужое"}, owner="someone-else")
    cycle = await open_cycle(base)

    reply = await cycle.call("aifa_state", {"artifact_ids": [foreign, "нет-такого"]})

    assert code_of(reply) == "ОТКАЗ неверные_аргументы" and "чужое" not in reply
    await cycle.close()


@pytest.mark.parametrize("ids", [["x"] * 21, [""], "x", [1]])
async def test_the_list_of_ids_is_bounded_and_made_of_ids(base, ids) -> None:
    cycle = await open_cycle(base)

    assert code_of(await cycle.call("aifa_state", {"artifact_ids": ids})) == "ОТКАЗ неверные_аргументы"
    await cycle.close()


async def test_documents_of_the_turn_are_listed_with_their_pages(base, documents, monkeypatch) -> None:
    """LR-36, C3.6: без аргументов — документы хода (id, имя, число страниц)."""
    cycle = await open_cycle(base, doc_ids=(DOC,))

    reply = await cycle.call("aifa_doc")

    assert code_of(reply) == "ГОТОВО" and f"{DOC} | ЛУКОЙЛ | страниц: 3" in reply
    assert read_tools.get_store().get_document(DOC).text_pages == 3
    monkeypatch.setattr(read_tools, "document_text", _unexpected_text)
    assert await cycle.call("aifa_doc") == reply
    await cycle.close()


def _unexpected_text(*args, **kwargs):
    raise AssertionError("Список уже знает число страниц")


@pytest.mark.parametrize(("scope", "count"), [("all_workspace", 1), ("conversation", 30)])
async def test_lists_documents_before_open(base, monkeypatch, scope, count):
    docs = {
        f"doc-{i:02d}": DocumentMeta(
            doc_id=f"doc-{i:02d}", filename=f"Отчёт {i:02d}.pdf", company_name="Реестр",
            meta_card={"issuer": "Компания", "period": "2024", "doc_kind": "Отчёт"},
        )
        for i in range(count)
    }
    for meta in list(docs.values())[1:]:
        meta.text_pages = 3
    monkeypatch.setattr(read_tools, "get_store", lambda: SimpleNamespace(get_document=docs.get))
    monkeypatch.setattr(read_tools, "document_text_exists", lambda doc_id: doc_id != "doc-00")
    monkeypatch.setattr(read_tools, "document_text", _unexpected_text)
    run = open_run(user_id=OWNER, case_id=CASE, doc_ids=tuple(docs), scope=scope)
    plugin = Plugin(ToolApi(Gateway(), None))
    ctx = ctx_of(run.run_id)
    try:
        first = (await plugin.call("aifa_doc", ctx)).text
        assert code_of(first) == "ГОТОВО"
        assert "Отчёт 00.pdf" in first and "не готов" in first
        assert "Компания" in first and "Реестр" not in first and "2024" in first
        if count > 1:
            offset = int(first.rsplit("next_offset=", 1)[1])
            last = (await plugin.call("aifa_doc", ctx, {"offset": offset})).text
            assert "doc-29" not in first and "doc-29" in last
            assert last.endswith("next_offset=null")
        assert code_of((await plugin.call("aifa_doc", ctx, {"doc_id": "doc-00"})).text) == "ОТКАЗ нет_цикла"
    finally:
        await plugin.close()
        await run.end()


@pytest.mark.parametrize(("scope", "workspace"), [("all_workspace", "folder"), ("conversation", None)])
@pytest.mark.parametrize("unavailable", [False, True])
async def test_ranks_the_turn_documents_before_open(base, monkeypatch, scope, workspace, unavailable):
    doc_ids = ("first", "second", "third", "fourth")
    docs = {doc_id: DocumentMeta(doc_id=doc_id, filename=f"{doc_id}.pdf") for doc_id in doc_ids}
    monkeypatch.setattr(read_tools, "get_store", lambda: SimpleNamespace(get_document=docs.get))
    monkeypatch.setattr(read_tools, "document_text_exists", lambda doc_id: False)
    called = []

    def select(ids, query, *, workspace_id):
        called.append((ids, query, workspace_id))
        if unavailable:
            raise SearchUnavailable("test outage")
        return [DocumentScore(doc_id, 1.0) for doc_id in ("third", "foreign", "first", "third")]

    monkeypatch.setattr(read_tools, "_default_search", lambda *_args, **_kw: SimpleNamespace(select_documents=select), raising=False)
    run = open_run(user_id=OWNER, case_id=CASE, doc_ids=doc_ids, scope=scope, workspace_id="folder")
    plugin = Plugin(ToolApi(Gateway(), None))
    try:
        reply = (await plugin.call("aifa_doc", ctx_of(run.run_id), {"query": "ВТБ"})).text
        assert code_of(reply) == "ГОТОВО"
        assert called == [(list(doc_ids), "ВТБ", workspace)]
        assert "foreign" not in reply
        rows = [row.partition(" | ")[0] for row in reply.splitlines() if row.split(" | ")[0] in doc_ids]
        assert rows == (list(doc_ids) if unavailable else ["third", "first", "second", "fourth"])
        assert ("ранжирование недоступно" in reply) == unavailable
    finally:
        await plugin.close()
        await run.end()


async def test_listing_counts_metric_facts_of_the_active_version(base, monkeypatch):
    metas = {
        name: DocumentMeta(doc_id=name, filename=f"{name}.pdf", text_pages=3) for name in ("A", "B")
    }
    monkeypatch.setattr(read_tools, "get_store", lambda: SimpleNamespace(get_document=metas.get))
    monkeypatch.setattr(read_tools, "document_text_exists", lambda doc_id: True)
    monkeypatch.setattr(read_tools, "document_text", _unexpected_text)

    base.ensure_case(owner_id=OWNER, case_id=CASE)
    for doc_id, version in (("A", "old-a"), ("A", "new-a"), ("B", "new-b")):
        base.register_document(
            owner_id=OWNER, case_id=CASE, logical_name=doc_id, document_id=doc_id, document_version=version,
        )
    current = (_metric_for_period("FY 2024", 10), _metric_for_period("FY 2023", 8))
    for node, version, facts in (
        ("read", "new-a", current),
        ("reuse", "new-a", current[:1]),
        ("legacy", "old-a", (_metric_for_period("FY 2024", 4),)),
    ):
        base.store.put_artifact(
            OWNER, CASE, node_key=NodeKey(atom_name="read.value", atom_version="1", args_fingerprint=node),
            output_type="ExtractedMetric", payloads=facts, concept="SALES_REVENUE", entity_ref=None,
            period_ref=None, basis=None, document_version=version, policy_version="test",
        )
    _put_dynamic(base, "note", {"text": "ещё один артефакт"})
    run = open_run(user_id=OWNER, case_id=CASE, doc_ids=("A", "B"))
    plugin = Plugin(ToolApi(Gateway(), None))
    try:
        reply = (await plugin.call("aifa_doc", ctx_of(run.run_id))).text
        rows = {row.partition(" | ")[0]: row for row in reply.splitlines()}
        assert "фактов: 2" in rows["A"]
        assert "фактов: 0" in rows["B"]
    finally:
        await plugin.close()
        await run.end()


async def test_the_text_of_a_page_range_is_recognized_text(base, documents) -> None:
    """C3.6: `doc_id` + `pages` — распознанный текст этих страниц, остальных нет."""
    cycle = await open_cycle(base, doc_ids=(DOC,))

    reply = await cycle.call("aifa_doc", {"doc_id": DOC, "pages": "2"})

    assert code_of(reply) == "ГОТОВО" and PAGES[2] in reply
    assert PAGES[1] not in reply and PAGES[3] not in reply
    await cycle.close()


async def test_a_search_finds_places_with_their_pages(base, documents) -> None:
    """C3.6: `doc_id` + `query` — страница и фрагмент каждого найденного места."""
    cycle = await open_cycle(base, doc_ids=(DOC,))

    reply = await cycle.call("aifa_doc", {"doc_id": DOC, "query": "ebitda"})

    assert "стр. 3:" in reply and "стр. 2:" not in reply
    await cycle.close()


async def test_a_document_of_another_turn_is_not_readable(base, documents) -> None:
    """LR-37, C3.6: чужой или неизвестный `doc_id` — `ОТКАЗ нет_документа`."""
    cycle = await open_cycle(base, doc_ids=(DOC,))

    assert code_of(await cycle.call("aifa_doc", {"doc_id": "doc-long"})) == "ОТКАЗ нет_документа"
    assert code_of(await cycle.call("aifa_doc", {"doc_id": "нет"})) == "ОТКАЗ нет_документа"
    await cycle.close()


async def test_a_long_text_comes_in_parts_with_the_next_page_named(base, documents) -> None:
    """LR-38, C3.6: ответ длиннее предела — частями, с номером страницы, с которой просить дальше."""
    cycle = await open_cycle(base, doc_ids=("doc-long",))

    reply = await cycle.call("aifa_doc", {"doc_id": "doc-long", "pages": "1-5"})

    assert code_of(reply) == "ГОТОВО" and len(reply) <= LIMIT
    assert "Следующие: aifa_doc doc_id=doc-long pages=" in reply
    await cycle.close()


@pytest.mark.parametrize("args", [{"pages": "1"}, {"doc_id": DOC, "pages": "abc"}, {"doc_id": DOC, "pages": "9"}])
async def test_a_wrong_request_is_a_refusal_with_the_way_out(base, documents, args) -> None:
    cycle = await open_cycle(base, doc_ids=(DOC,))

    assert code_of(await cycle.call("aifa_doc", args)) == "ОТКАЗ неверные_аргументы"
    await cycle.close()
