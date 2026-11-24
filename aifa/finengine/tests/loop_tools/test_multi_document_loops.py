"""C3.13: a report loop is opened, serialized and stored by its document."""
# ruff: noqa: F401, F811 — импортированные фикстуры pytest

from __future__ import annotations

import asyncio
import threading

import pytest
from test_open_and_restore import CHAT, DOC, MOVE, OWNER, LoopDesk, LoopOpening, agent_leads, gate_env, severstal_text, turn
from loop_support import Gateway
from tool_harness import Plugin, ctx_of, first_line

from finengine.assistant.loop_tools import MoveTool
from finengine.assistant.tool_api import ToolApi
from finengine.config import settings
from finengine.models.document import DocumentMeta
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
