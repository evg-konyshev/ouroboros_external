"""C3.13: a report loop is opened, serialized and stored by its document."""
# ruff: noqa: F401, F811 — импортированные фикстуры pytest

from __future__ import annotations

import asyncio
import json
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest
from test_open_and_restore import (
    CHAT, DOC, MOVE, OWNER, LoopDesk, LoopOpening, _make_fake_locate_and_read_value_executors,
    agent_leads, gate_env, patched_build_registry, severstal_text, turn,
)
from loop_support import Gateway
from tool_harness import ROOT, Plugin, ctx_of, first_line

from finengine.assistant.jobs import JobBoard
from finengine.assistant.loop_tools import MoveTool
from finengine.assistant.tool_api import ToolApi
from finengine.config import settings
from finengine.models.document import DocumentMeta
from finengine.skills.case_runtime import CaseRuntime
from finengine.store import blobs, state
from finengine.store.documents import SqlDocumentStore

B = "doc-b"
C = "doc-c"


@pytest.fixture
def registry(tmp_path, monkeypatch, severstal_text):
    url = f"sqlite:///{tmp_path}/documents.db"
    monkeypatch.setattr(settings, "documents_database_url", url)
    store = SqlDocumentStore(url)
    for name in (DOC, B, C):
        store.add_document(DocumentMeta(doc_id=name, filename=f"{name}.pdf", owner_id=OWNER), [])
    monkeypatch.setattr(blobs, "document_text", lambda doc_id, **kw: {
        DOC: severstal_text, B: severstal_text + "\nВторой отчёт.",
    }.get(doc_id))
    return store


@pytest.mark.parametrize("concurrent", [False, True])
async def test_a_turn_opens_one_loop_per_document(registry, concurrent):
    run = turn(doc_ids=(DOC, B, C), scope="all_workspace")
    desk = LoopDesk()
    plugin = Plugin(ToolApi(Gateway(), LoopOpening(desk), desk))
    ctx = ctx_of(run.run_id, owner_id=str(OWNER), chat_id=CHAT)
    try:
        if concurrent:
            replies = await asyncio.gather(*(plugin.call("aifa_open", ctx, {"doc_id": doc}) for doc in (DOC, B)))
        else:
            replies = [await plugin.call("aifa_open", ctx, {"doc_id": doc}) for doc in (DOC, B)]
        assert [first_line(reply) for reply in replies] == ["ГОТОВО", "ГОТОВО"]
        first, second = desk.live(run.run_id, DOC), desk.live(run.run_id, B)
        assert first is not None and second is not None and first.session is not second.session
        assert {doc.document_id for doc in first.session.runtime.store.active_documents(OWNER, CHAT)} == {DOC, B}
        index = state.get(state.LOOP_INDEX, owner=OWNER, key=run.run_id)
        assert index is not None and index["doc_ids"] == [DOC, B]

        meta = registry.get_document(DOC)
        assert meta is not None and meta.profile_id == first.session.run_passport.classification.profile_id
        meta.company_name = "Переопределённое имя"
        registry.add_document(meta, [])
        assert registry.get_document(DOC).profile_id == meta.profile_id
    finally:
        await plugin.close()
        await run.end()


async def test_implicit_and_explicit_open_share_a_report_loop(registry):
    run = turn()
    desk = LoopDesk()
    plugin = Plugin(ToolApi(Gateway(), LoopOpening(desk), desk))
    ctx = ctx_of(run.run_id, owner_id=str(OWNER), chat_id=CHAT)
    try:
        assert first_line(await plugin.call("aifa_open", ctx)) == "ГОТОВО"
        session = desk.live(run.run_id).session
        assert first_line(await plugin.call("aifa_open", ctx, {"doc_id": DOC})) == "ГОТОВО"
        assert desk.live(run.run_id, DOC).session is session
    finally:
        await plugin.close()
        await run.end()


@pytest.mark.parametrize(("args", "reason"), [({}, "doc_id"), ({"doc_id": C}, "не готов"), ({"doc_id": "Z"}, "ход")])
async def test_open_refuses_a_missing_or_unready_document(registry, args, reason):
    run = turn(doc_ids=(DOC, B, C), scope="all_workspace")
    desk = LoopDesk()
    plugin = Plugin(ToolApi(Gateway(), LoopOpening(desk), desk))
    try:
        reply = await plugin.call("aifa_open", ctx_of(run.run_id, owner_id=str(OWNER), chat_id=CHAT), args)
        assert first_line(reply) == "ОТКАЗ нет_документа" and reason in reply.text
    finally:
        await plugin.close()
        await run.end()


async def test_open_waits_for_the_move_on_the_other_document(registry, monkeypatch):
    run = turn(doc_ids=(DOC, B), scope="all_workspace")
    desk = LoopDesk()
    plugin = Plugin(ToolApi(Gateway(), LoopOpening(desk), desk))
    ctx = ctx_of(run.run_id, owner_id=str(OWNER), chat_id=CHAT)
    began, release = threading.Event(), threading.Event()
    execute = MoveTool._execute

    def held(tool, loop, args):
        began.set()
        assert release.wait(timeout=5)
        return execute(tool, loop, args)

    monkeypatch.setattr(MoveTool, "_execute", held)
    try:
        assert first_line(await plugin.call("aifa_open", ctx, {"doc_id": DOC})) == "ГОТОВО"
        move = asyncio.create_task(plugin.call("aifa_move", ctx, MOVE))
        assert await asyncio.to_thread(began.wait, 2)
        monkeypatch.setattr(settings, "ouroboros_goal_wait_sec", 0.05)
        queued = await plugin.call("aifa_open", ctx, {"doc_id": B})
        assert first_line(queued).startswith("ЖДИ job=")
        assert desk.live(run.run_id, B) is None
        release.set()
        assert first_line(await move) == "ГОТОВО"
        job_id = first_line(queued).split("=", 1)[1]
        assert first_line(await plugin.call("aifa_wait", ctx, {"job_id": job_id, "wait_sec": 2})) == "ГОТОВО"
    finally:
        release.set()
        await plugin.close()
        await run.end()


async def test_a_started_report_open_allows_the_document_listing(registry, monkeypatch):
    run = turn(doc_ids=(DOC,), scope="conversation")
    desk = LoopDesk()
    plugin = Plugin(ToolApi(Gateway(), LoopOpening(desk), desk))
    ctx = ctx_of(run.run_id, owner_id=str(OWNER), chat_id=CHAT)
    began, release = asyncio.Event(), asyncio.Event()

    async def held(doc_id):
        began.set()
        await release.wait()
        return blobs.document_text(doc_id)

    monkeypatch.setattr("finengine.assistant.loop_opening._document_text", held)
    try:
        opening = asyncio.create_task(plugin.call("aifa_open", ctx, {"doc_id": DOC}))
        await asyncio.wait_for(began.wait(), timeout=2)
        assert first_line(await plugin.call("aifa_doc", ctx)) == "ГОТОВО"
        release.set()
        assert first_line(await opening) == "ГОТОВО"
    finally:
        release.set()
        await plugin.close()
        await run.end()


async def test_a_seventh_report_is_refused_without_changing_existing_loops(registry, monkeypatch, severstal_text):
    reports = tuple(f"report-{i}" for i in range(7))
    for doc_id in reports:
        registry.add_document(DocumentMeta(doc_id=doc_id, filename=f"{doc_id}.pdf", owner_id=OWNER), [])
    monkeypatch.setattr(blobs, "document_text", lambda doc_id, **kw: f"{severstal_text}\n{doc_id}")
    run = turn(doc_ids=reports, scope="all_workspace")
    desk = LoopDesk()
    plugin = Plugin(ToolApi(Gateway(), LoopOpening(desk), desk))
    ctx = ctx_of(run.run_id, owner_id=str(OWNER), chat_id=CHAT)
    try:
        for doc_id in reports[:6]:
            assert first_line(await plugin.call("aifa_open", ctx, {"doc_id": doc_id})) == "ГОТОВО"
        reply = await plugin.call("aifa_open", ctx, {"doc_id": reports[6]})
        assert first_line(reply) == "ОТКАЗ лимит_циклов"
        assert desk.live(run.run_id, reports[6]) is None
        assert desk.report_ids(run.run_id, OWNER) == reports[:6]
        mover = MoveTool(JobBoard(0.05), desk)
        for doc_id in reports[:6]:
            loop = desk.live(run.run_id, doc_id)
            assert loop is not None and loop.session.turns == 0
            result = await asyncio.to_thread(mover._execute, loop, MOVE)
            assert result.startswith("ГОТОВО") and loop.session.turns == 1
    finally:
        await plugin.close()
        await run.end()


@pytest.mark.parametrize("market_first", [False, True])
async def test_report_and_market_loops_cannot_mix(registry, market_first):
    run = turn()
    desk = LoopDesk()
    plugin = Plugin(ToolApi(Gateway(), LoopOpening(desk), desk))
    ctx = ctx_of(run.run_id, owner_id=str(OWNER), chat_id=CHAT)
    market, report = {"mode": "market"}, {"doc_id": DOC}
    first, second = (market, report) if market_first else (report, market)
    try:
        assert first_line(await plugin.call("aifa_open", ctx, first)) == "ГОТОВО"
        assert first_line(await plugin.call("aifa_open", ctx, second)) == "ОТКАЗ неверные_аргументы"
        assert desk.report_ids(run.run_id, OWNER) == (() if market_first else (DOC,))
    finally:
        await plugin.close()
        await run.end()


@pytest.fixture
async def two_loops(registry):
    run = turn(doc_ids=(DOC, B, C), scope="all_workspace")
    desk = LoopDesk()
    plugin = Plugin(ToolApi(Gateway(), LoopOpening(desk), desk))
    ctx = ctx_of(run.run_id, owner_id=str(OWNER), chat_id=CHAT)
    try:
        for doc_id in (DOC, B):
            assert first_line(await plugin.call("aifa_open", ctx, {"doc_id": doc_id})) == "ГОТОВО"
        yield run, desk, plugin, ctx
    finally:
        await plugin.close()
        await run.end()


async def test_two_loops_require_an_address_without_spending_a_move(two_loops):
    run, desk, plugin, ctx = two_loops
    sub = {**ctx, "task_id": "author", "root_task_id": ROOT, "task_depth": 1}
    for tool, call_ctx, args in (
        ("aifa_move", ctx, MOVE),
        ("aifa_register_atom", sub, {}),
        ("aifa_atoms", ctx, {"action": "list"}),
        ("aifa_report", sub, {"label": "статус", "status": "ok"}),
    ):
        reply = await plugin.call(tool, call_ctx, args)
        assert first_line(reply) == "ОТКАЗ неверные_аргументы"
        assert DOC in reply.text and B in reply.text
    for doc_id in (DOC, B):
        assert (desk.live(run.run_id, doc_id).session.turns, desk.live(run.run_id, doc_id).session.rejected) == (0, 0)
    assert first_line(await plugin.call("aifa_open", sub, {"doc_id": DOC})) == "ОТКАЗ не_та_роль"


async def test_named_moves_and_case_wide_reads(two_loops):
    run, desk, plugin, ctx = two_loops
    for name in (DOC, B, DOC):
        reply = await plugin.call("aifa_move", ctx, {**MOVE, "doc_id": name})
        assert first_line(reply) == "ГОТОВО", reply.text

    a = desk.live(run.run_id, DOC)
    b = desk.live(run.run_id, B)
    assert (a.session.turns, b.session.turns) == (2, 1)
    rows = a.session.runtime.store.list_artifacts(OWNER, CHAT)
    metrics = {row["document_version"]: row["evidence_ids"][0] for row in rows if row["output_type"] == "ExtractedMetric"}
    ids = [metrics[loop.session.target.document_version] for loop in (a, b)]
    reply = await plugin.call("aifa_state", ctx, {"artifact_ids": ids})
    assert all(evidence_id in reply.text for evidence_id in ids)
    assert first_line(await plugin.call("aifa_atoms", ctx, {"action": "list", "doc_id": B})) == "ГОТОВО"
    assert first_line(await plugin.call("aifa_state", ctx, {"doc_id": B})) == "ГОТОВО"


async def test_a_deleted_document_is_refused_before_the_move(two_loops, registry):
    run, desk, plugin, ctx = two_loops
    a = desk.live(run.run_id, DOC)
    catalog = a.session.runtime.store
    before = catalog.active_documents(OWNER, CHAT)
    registry.remove_document(DOC)

    assert first_line(await plugin.call("aifa_move", ctx, {**MOVE, "doc_id": DOC})) == "ОТКАЗ нет_документа"
    assert a.session.turns == 0 and catalog.active_documents(OWNER, CHAT) == before


async def test_an_author_registers_only_in_the_named_loop(two_loops, local_room):
    run, desk, plugin, ctx = two_loops
    sub = {**ctx, "task_id": "author", "root_task_id": ROOT, "task_depth": 1}
    assert first_line(await plugin.call("aifa_move", ctx, {**MOVE, "doc_id": B})) == "ГОТОВО"
    args = {
        "doc_id": B, "name": "eph.scoped_note", "purpose": "Отчёт по выбранному документу",
        "requires": [{"input_type": "ExtractedMetric", "concept": "SALES_REVENUE"}],
        "output_type": "dyn:scoped_note", "output_schema": {"type": "object"},
        "code": "def run(inputs):\n    return {'note': 'Готово'}\n",
    }
    reply = await plugin.call("aifa_register_atom", sub, args)
    assert first_line(reply) == "ГОТОВО", reply.text
    assert "eph.scoped_note" not in desk.live(run.run_id, DOC).session.atoms
    assert "eph.scoped_note" in desk.live(run.run_id, B).session.atoms
    listed = await plugin.call("aifa_atoms", sub, {"action": "list", "doc_id": B})
    assert "eph.scoped_note" in listed.text
    assert first_line(await plugin.call("aifa_report", sub, {"doc_id": B, "label": "готово", "status": "done"})) == "ГОТОВО"


async def test_named_moves_share_the_turn_queue(two_loops, monkeypatch):
    _, _, plugin, ctx = two_loops
    began, release, second_began = threading.Event(), threading.Event(), threading.Event()
    execute = MoveTool._execute

    def held(tool, loop, args):
        if loop.doc_id == DOC:
            began.set()
            assert release.wait(timeout=5)
        else:
            second_began.set()
        return execute(tool, loop, args)

    monkeypatch.setattr(MoveTool, "_execute", held)
    first = asyncio.create_task(plugin.call("aifa_move", ctx, {**MOVE, "doc_id": DOC}))
    try:
        assert await asyncio.to_thread(began.wait, 2)
        monkeypatch.setattr(settings, "ouroboros_goal_wait_sec", 0.05)
        second = await plugin.call("aifa_move", ctx, {**MOVE, "doc_id": B})
        assert first_line(second).startswith("ЖДИ job=") and not second_began.is_set()
        release.set()
        assert first_line(await first) == "ГОТОВО"
        job_id = first_line(second).split("=", 1)[1]
        assert first_line(await plugin.call("aifa_wait", ctx, {"job_id": job_id, "wait_sec": 2})) == "ГОТОВО"
        assert second_began.is_set()
    finally:
        release.set()
        await first


async def test_alternating_moves_pass_their_frozen_passports(registry, monkeypatch):
    fixtures = Path(__file__).resolve().parents[1] / "golden" / "classification" / "cases.json"
    examples = json.loads(fixtures.read_text(encoding="utf-8"))
    documents = {
        doc_id: next(case["excerpt"] for case in examples if case["profile_id"] == profile)
        for doc_id, profile in ((DOC, "ifrs_bank_ru"), (B, "rsbu_nf_ru"))
    }
    monkeypatch.setattr(blobs, "document_text", lambda doc_id, **kw: documents.get(doc_id))
    locate, read = _make_fake_locate_and_read_value_executors({"TOTAL_ASSETS": 100.0})
    patched_build_registry(monkeypatch, locate, read)
    planned = []
    plan = CaseRuntime.plan

    def capture(runtime, **kwargs):
        planned.append(kwargs["run_passport"].classification.profile_id)
        return plan(runtime, **kwargs)

    monkeypatch.setattr(CaseRuntime, "plan", capture)
    run = turn(doc_ids=(DOC, B), scope="all_workspace")
    desk = LoopDesk()
    plugin = Plugin(ToolApi(Gateway(), LoopOpening(desk), desk))
    ctx = ctx_of(run.run_id, owner_id=str(OWNER), chat_id=CHAT)
    try:
        for doc_id in (DOC, B):
            assert first_line(await plugin.call("aifa_open", ctx, {"doc_id": doc_id})) == "ГОТОВО"
        for step, doc_id in enumerate((DOC, B, DOC)):
            goal = {**MOVE["goals"][0], "ref": f"g{step}", "concepts": ["TOTAL_ASSETS"]}
            reply = await plugin.call("aifa_move", ctx, {**MOVE, "doc_id": doc_id, "goals": [goal]})
            assert first_line(reply) == "ГОТОВО", reply.text
        assert planned == ["ifrs_bank_ru", "rsbu_nf_ru", "ifrs_bank_ru"]
    finally:
        await plugin.close()
        await run.end()


async def test_one_finish_closes_both_loops_and_announces_both_documents(two_loops):
    run, desk, plugin, ctx = two_loops
    for doc_id in (DOC, B):
        assert first_line(await plugin.call("aifa_move", ctx, {**MOVE, "doc_id": doc_id})) == "ГОТОВО"
    first, second = desk.live(run.run_id, DOC), desk.live(run.run_id, B)
    rows = first.session.runtime.store.list_artifacts(OWNER, CHAT)
    by_version = {row["document_version"]: row["evidence_ids"][0] for row in rows if row["output_type"] == "ExtractedMetric"}
    metrics = [by_version[loop.session.target.document_version] for loop in (first, second)]

    result = await plugin.call("aifa_finish", ctx, {"action": "finalize", "evidence_ids": metrics})
    assert first_line(result) == "ПРИНЯТО"
    assert all(f"{doc_id}.pdf" in result.text for doc_id in (DOC, B))
    assert first.session.closed and second.session.closed
    for doc_id in (DOC, B):
        assert first_line(await plugin.call("aifa_move", ctx, {**MOVE, "doc_id": doc_id})) == "ОТКАЗ цикл_закрыт"
    assert first_line(await plugin.call("aifa_finish", ctx, {"action": "give_up"})) == "ОТКАЗ цикл_закрыт"
    assert first_line(await plugin.call("aifa_open", ctx, {"doc_id": DOC})) == "ОТКАЗ цикл_закрыт"
    await run.end()
    events = await run.events()
    documents = [e for e in events if e.get("type") == "turn_documents"]
    assert documents == [{"type": "turn_documents", "document_ids": [DOC, B]}]
    first_card = next(i for i, event in enumerate(events) if event.get("type") == "typed_result")
    assert events.index(documents[0]) < first_card


async def test_reports_share_one_deadline_after_restore(registry, monkeypatch):
    from finengine.assistant import loop_desk, subtask_waits
    from finengine.skills.spec import dynamic_run, loop_state

    now = 0.0

    def monotonic_now():
        return 10_000.0 + now

    async def advance(seconds):
        nonlocal now
        now += seconds
        await asyncio.sleep(0)

    for module in (loop_desk, subtask_waits, dynamic_run, loop_state):
        monkeypatch.setattr(module, "monotonic", monotonic_now, raising=False)
    monkeypatch.setattr(loop_desk, "time", lambda: 1_800_000_000.0 + now, raising=False)
    monkeypatch.setattr(subtask_waits, "asyncio", SimpleNamespace(sleep=advance))
    monkeypatch.setattr(subtask_waits, "POLL_SEC", 30)
    monkeypatch.setattr(settings, "ouroboros_loop_max_wall_sec", 1200)
    run = turn(doc_ids=(DOC, B), scope="all_workspace")
    desk = LoopDesk()
    plugin = Plugin(ToolApi(Gateway(), LoopOpening(desk), desk))
    recovered = LoopDesk()
    after = Plugin(ToolApi(Gateway(), LoopOpening(recovered), recovered))
    ctx = ctx_of(run.run_id, owner_id=str(OWNER), chat_id=CHAT)
    try:
        assert first_line(await plugin.call("aifa_open", ctx, {"doc_id": DOC})) == "ГОТОВО"
        assert desk.live(run.run_id, DOC).session.limits.max_wall_seconds == 1200
        now = 900
        assert first_line(await plugin.call("aifa_open", ctx, {"doc_id": B})) == "ГОТОВО"
        assert desk.live(run.run_id, B).session.limits.max_wall_seconds == 300
        index = state.get(state.LOOP_INDEX, owner=OWNER, key=run.run_id)
        assert index["deadline"] == 1_800_001_200.0

        now = 1080
        assert first_line(await after.call("aifa_state", ctx, {"doc_id": B})) == "ГОТОВО"
        assert recovered.live(run.run_id, B).session.limits.max_wall_seconds == 300
        assert first_line(await after.call("aifa_wait", ctx, {"task_id": "author", "wait_sec": 240})) == "ЛИМИТ"
        assert now == 1200
        now = 1260
        for doc_id in (DOC, B):
            assert first_line(await after.call("aifa_move", ctx, {**MOVE, "doc_id": doc_id})) == "ЛИМИТ"
    finally:
        await after.close()
        await plugin.close()
        await run.end()


async def test_state_overview_reads_saved_metadata_while_the_turn_queue_is_held(two_loops, monkeypatch):
    run, desk, plugin, ctx = two_loops
    await plugin.call("aifa_move", ctx, {**MOVE, "doc_id": B})
    sub = {**ctx, "task_id": "plan-author", "root_task_id": ROOT, "task_depth": 1}
    restarted = LoopDesk()
    after = Plugin(ToolApi(Gateway(), LoopOpening(restarted), restarted))

    def forbidden(*args, **kw):
        raise AssertionError("Обзор хода не загружает текст отчёта")

    monkeypatch.setattr(blobs, "document_text", forbidden)
    try:
        async with desk.queue_of(run.run_id):
            reply = await asyncio.wait_for(plugin.call("aifa_state", sub), 2)
        assert first_line(reply) == "ГОТОВО"
        assert "Обзор хода" in reply.text and "Срок" in reply.text
        assert f"{DOC}.pdf" in reply.text and f"{B}.pdf" in reply.text and C in reply.text
        assert "ifrs_nf_ru" in reply.text and "0 | 12" in reply.text and "1 | 11" in reply.text
        assert "Реестр показателей" not in reply.text and "Полученные артефакты" not in reply.text
        assert (await after.call("aifa_state", sub)).text == reply.text
        named = await plugin.call("aifa_state", sub, {"doc_id": B})
        assert first_line(named) == "ГОТОВО" and "Реестр показателей" in named.text
    finally:
        await after.close()
