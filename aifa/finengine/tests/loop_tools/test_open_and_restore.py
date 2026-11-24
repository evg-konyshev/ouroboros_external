"""Цикл открывает `aifa_open` и поднимает сохранённое после перезапуска (ADR-0023, ADR-0060 пп. 1–2).

Настоящие `LoopOpening`, `open_agent_loop`, `GoalChannel` → `execute_routed_goal`, `CaseRuntime`, `LoopDesk` и
хранилище состояния; подставные — пара `locate`/`read.value` и документ (опоры сквозных тестов чата,
`tests/integration`). Модель-классификатор и модель планировщика службы вызывать нельзя.
"""
# ruff: noqa: F811 — `gate_env` и `severstal_text` подключены как фикстуры
from __future__ import annotations

import pytest
from loop_support import Gateway
from tests.integration.conftest import (  # noqa: F401
    _make_fake_locate_and_read_value_executors,
    severstal_text,
)
from tests.integration.ingress_support import (  # noqa: F401
    DOC,
    OWNER,
    gate_env,
    patched_build_registry,
)
from tool_harness import Plugin, ctx_of, first_line, open_run

from finengine.assistant.loop_desk import LoopDesk
from finengine.assistant.loop_opening import LoopOpening, NoDocument
from finengine.assistant.tool_api import ToolApi
from finengine.config import settings
from finengine.ingestion.document_store import get_store
from finengine.models.document import DocumentMeta
from finengine.skills.spec.dynamic_run import LoopSession
from finengine.store import blobs, state
from finengine.store.documents import SqlDocumentStore

CHAT = "p-lukoil"
MESSAGE = "какая выручка за 2024 год?"
REVENUE = {"ref": "g1", "scenario": "extract_registered_metric", "concepts": ["SALES_REVENUE"], "period": "p-2024-fy"}
MOVE = {"objective": "получить выручку", "reason": "нужна для ответа", "goals": [REVENUE]}


async def _forbidden(*args, **kwargs):
    raise AssertionError("ADR-0060: сообщение не разбирает модель, цикл не ведёт модель службы")


@pytest.fixture(autouse=True)
def agent_leads(monkeypatch: pytest.MonkeyPatch, coordination, tmp_path) -> None:
    monkeypatch.setattr("finengine.skills.atoms.intent._default_complete", _forbidden)
    monkeypatch.setattr("finengine.skills.spec.dynamic_run._default_complete", _forbidden)
    locate, read_value = _make_fake_locate_and_read_value_executors({"SALES_REVENUE": 987_654.0})
    patched_build_registry(monkeypatch, locate, read_value)
    url = f"sqlite:///{tmp_path}/documents.db"
    monkeypatch.setattr(settings, "documents_database_url", url)
    SqlDocumentStore(url).add_document(DocumentMeta(doc_id=DOC, filename=f"{DOC}.pdf", owner_id=OWNER), [])


def turn(**changes):
    fields = {"user_id": OWNER, "owner_id": str(OWNER), "chat_id": CHAT, "case_id": CHAT, "doc_ids": (DOC,),
              "message": MESSAGE}
    return open_run(**{**fields, **changes})


async def opened(desk: LoopDesk | None = None, **changes):
    """Цикл открыт `aifa_open`; дальше его ведёт клиент-ouroboros."""
    desk = desk or LoopDesk()
    run = turn(**changes)
    plugin = Plugin(ToolApi(Gateway(), LoopOpening(desk), desk))
    ctx = ctx_of(run.run_id, owner_id=str(OWNER), chat_id=CHAT)
    reply = await plugin.call("aifa_open", ctx)
    assert first_line(reply) == "ГОТОВО", reply.text
    return run, plugin, ctx, desk


async def test_aifa_open_opens_the_cycle_without_parsing_the_message() -> None:
    """ADR-0060 п. 1, LR-40: цикл открыт без разбора и конвейера — ходов нет, профиль и реестр в состоянии."""
    desk = LoopDesk()
    run = turn()

    session = await LoopOpening(desk)(run.record)

    assert isinstance(session, LoopSession) and desk.live(run.run_id).session is session
    assert session.turns == 0 and session.clauses == {} and session.entry_stage == "agent"
    assert session.run_passport is not None, "профиль отчёта и реестр зафиксированы паспортом"
    assert session.goal.raw_query == MESSAGE
    await run.end()


@pytest.mark.parametrize(("changes", "said"), [
    ({"doc_ids": ()}, "приложить"),
    ({"doc_ids": (DOC, "doc-2"), "active_doc_id": ""}, "выбрать один"),
    ({"doc_ids": ("doc-unknown",)}, "не готов"),
])
async def test_aifa_open_without_a_report_to_open_on_says_what_to_ask(changes, said, monkeypatch) -> None:
    """§3.1: нет документа, не выбран активный или текст не распознан — `нет_документа` с тем, что сказать."""
    monkeypatch.setattr("finengine.assistant.loop_opening.TEXT_WAIT_SEC", 0.1)
    monkeypatch.setattr("finengine.assistant.loop_opening.TEXT_POLL_SEC", 0.05)
    run = turn(**changes)

    found = await LoopOpening(LoopDesk())(run.record)

    assert isinstance(found, NoDocument) and said in found.why
    await run.end()


async def test_the_role_chosen_in_the_interface_is_the_role_of_the_cycle() -> None:
    """Роль интерфейса — точным кодом: часть цели без `role` берёт её (ADR-0060 п. 2)."""
    run = turn(role="risks")

    session = await LoopOpening(LoopDesk())(run.record)

    assert isinstance(session, LoopSession) and session.goal.role == "risks"
    await run.end()


async def test_the_cycle_survives_a_restart_of_the_process() -> None:
    """ADR-0023: состояние сохранено после открытия; новый процесс (пустой `LoopDesk`) поднимает его и ведёт дальше."""
    run, plugin, ctx, _ = await opened()
    assert state.get(state.LOOP_SESSION, owner=OWNER, key=f"{run.run_id}/{DOC}") is not None

    restarted = LoopDesk()
    after = Plugin(ToolApi(Gateway(), LoopOpening(restarted), restarted))
    reading = await after.call("aifa_state", ctx)
    closed = await after.call("aifa_finish", ctx, {"action": "give_up"})

    assert first_line(reading) == "ГОТОВО" and MESSAGE in reading.text
    assert first_line(closed) == "ПРИНЯТО" and restarted.live(run.run_id).session.closed
    await plugin.close()
    await after.close()
    await run.end()


async def test_a_turn_of_another_owner_finds_no_saved_cycle() -> None:
    """Граница владельца: сохранённый цикл читается только под ключом владельца хода."""
    run, plugin, _, _ = await opened()

    assert state.get(state.LOOP_SESSION, owner=OWNER + 1, key=f"{run.run_id}/{DOC}") is None
    await plugin.close()
    await run.end()


async def test_a_goal_of_a_move_goes_the_same_pipeline_as_a_chat_message() -> None:
    """LR-13, LR-15: часть цели агента идёт тем же конвейером; на экран из неё — только `plan_graph`/`node_run`."""
    run, plugin, ctx, _ = await opened()

    reply = await plugin.call("aifa_move", ctx, MOVE)
    await run.end()
    kinds = {event["type"] for event in await run.events()}

    assert first_line(reply) == "ГОТОВО" and "✓ extract_registered_metric" in reply.text
    assert "987654" in reply.text.replace(" ", "").replace(" ", ""), "значение — в новых артефактах хода"
    assert {"plan_graph", "node_run", "dynamic_move"} <= kinds
    assert not kinds & {"typed_result", "typed_error"}, "карточки и отказы конвейера пользователю не видны"
    await plugin.close()


async def test_a_goal_without_a_deterministic_root_is_refused_before_the_pipeline() -> None:
    """LR-14: у сценария нет детерминированного корня — отказ хода с причиной, не вложенный цикл; ход не потрачен."""
    run, plugin, ctx, desk = await opened()

    reply = await plugin.call("aifa_move", ctx, {**MOVE, "goals": [{"ref": "g9", "scenario": "custom_analysis"}]})

    assert first_line(reply) == "ОТКАЗ неверные_аргументы" and "нет детерминированного пути" in reply.text
    assert desk.live(run.run_id).session.turns == 0
    await plugin.close()
    await run.end()


async def test_every_move_is_saved_and_the_restored_cycle_goes_on_from_it() -> None:
    """ADR-0023: состояние сохраняется после каждого хода; после перезапуска части цели, журнал и счётчики — те же."""
    run, plugin, ctx, _ = await opened()
    await plugin.call("aifa_move", ctx, MOVE)
    saved = state.get(state.LOOP_SESSION, owner=OWNER, key=f"{run.run_id}/{DOC}")["state"]
    assert saved["turns"] == 1 and saved["journal"][0]["objective"] == "получить выручку"

    restarted = LoopDesk()
    after = Plugin(ToolApi(Gateway(), LoopOpening(restarted), restarted))
    reading = await after.call("aifa_state", ctx)
    second = await after.call("aifa_move", ctx, {**MOVE, "goals": ["g1"]})

    assert "получить выручку" in reading.text and "ходов: 11 из 12" in reading.text
    assert "- g1: extract_registered_metric" in reading.text, "часть цели агента пережила перезапуск"
    assert first_line(second) == "ГОТОВО" and "## Итог хода 2" in second.text
    assert restarted.live(run.run_id).session.turns == 2
    await plugin.close()
    await after.close()
    await run.end()


async def test_agent_loop_uses_the_configured_wall_budget(monkeypatch):
    from finengine.config import settings

    monkeypatch.setattr(settings, "ouroboros_loop_max_wall_sec", 2400.0)
    run = turn()
    session = await LoopOpening(LoopDesk())(run.record)
    assert isinstance(session, LoopSession)
    assert session.limits.max_wall_seconds == 2400.0
    await run.end()


SECOND = "doc-second"


@pytest.fixture
async def saved_pair(monkeypatch, severstal_text):
    get_store().add_document(DocumentMeta(doc_id=SECOND, filename="Другой отчёт.pdf", owner_id=OWNER), [])
    text = {DOC: severstal_text, SECOND: severstal_text + "\nВторой документ."}
    monkeypatch.setattr(blobs, "document_text", lambda doc_id, **kw: text.get(doc_id))
    run, plugin, ctx, desk = await opened(doc_ids=(DOC, SECOND), active_doc_id=DOC, scope="all_workspace")
    try:
        assert first_line(await plugin.call("aifa_open", ctx, {"doc_id": SECOND})) == "ГОТОВО"
        yield run, plugin, ctx, desk, text
    finally:
        await plugin.close()
        await run.end()


async def test_named_report_loop_survives_a_restart_and_finish_closes_both(saved_pair):
    run, plugin, ctx, _, _ = saved_pair
    assert first_line(await plugin.call("aifa_move", ctx, {**MOVE, "doc_id": SECOND})) == "ГОТОВО"
    restored = LoopDesk()
    after = Plugin(ToolApi(Gateway(), LoopOpening(restored), restored))
    try:
        reply = await after.call("aifa_state", ctx, {"doc_id": SECOND})
        assert first_line(reply) == "ГОТОВО" and MOVE["objective"] in reply.text
        assert restored.live(run.run_id, SECOND).session.turns == 1
        assert first_line(await after.call("aifa_finish", ctx, {"action": "give_up"})) == "ПРИНЯТО"
        for doc_id in (DOC, SECOND):
            assert state.get(state.LOOP_SESSION, owner=OWNER, key=f"{run.run_id}/{doc_id}")["state"]["closed"]
    finally:
        await after.close()


async def test_report_with_missing_text_is_not_restored(saved_pair):
    run, _, ctx, original, text = saved_pair
    before = original.live(run.run_id, SECOND).session.runtime.store.active_documents(OWNER, CHAT)
    del text[SECOND]
    restored = LoopDesk()
    after = Plugin(ToolApi(Gateway(), LoopOpening(restored), restored))
    try:
        reply = await after.call("aifa_state", ctx, {"doc_id": SECOND})
        assert first_line(reply) == "ОТКАЗ нет_цикла"
        assert restored.live(run.run_id, SECOND) is None
        assert original.live(run.run_id, SECOND).session.runtime.store.active_documents(OWNER, CHAT) == before
        finished = await after.call("aifa_finish", ctx, {"action": "give_up"})
        assert first_line(finished) == "ПРИНЯТО" and "Другой отчёт.pdf" in finished.text
        assert state.get(state.LOOP_SESSION, owner=OWNER, key=f"{run.run_id}/{SECOND}")["state"]["closed"]
    finally:
        await after.close()


async def test_sweep_deletes_saved_loops_that_were_not_restored_locally(saved_pair):
    run, _, ctx, _, _ = saved_pair
    restored = LoopDesk()
    after = Plugin(ToolApi(Gateway(), LoopOpening(restored), restored))
    try:
        assert first_line(await after.call("aifa_state", ctx, {"doc_id": SECOND})) == "ГОТОВО"
        assert restored.live(run.run_id, DOC) is None
        await run.end()
        restored._sweep()
        assert state.get(state.LOOP_INDEX, owner=OWNER, key=run.run_id) is None
        for doc_id in (DOC, SECOND):
            assert state.get(state.LOOP_SESSION, owner=OWNER, key=f"{run.run_id}/{doc_id}") is None
    finally:
        await after.close()


async def test_finish_after_an_interrupted_snapshot_write_publishes_once(saved_pair, monkeypatch):
    run, plugin, ctx, _, _ = saved_pair
    put = state.put
    failed = False

    def fail_once(kind, *, owner, key, doc):
        nonlocal failed
        if kind == state.LOOP_SESSION and key.endswith(f"/{SECOND}") and doc["state"]["closed"] and not failed:
            failed = True
            raise OSError("closing second snapshot interrupted")
        put(kind, owner=owner, key=key, doc=doc)

    monkeypatch.setattr(state, "put", fail_once)
    assert (await plugin.call("aifa_finish", ctx, {"action": "give_up"})).status_code == 500
    assert state.get(state.LOOP_SESSION, owner=OWNER, key=f"{run.run_id}/{DOC}")["state"]["closed"]
    assert not state.get(state.LOOP_SESSION, owner=OWNER, key=f"{run.run_id}/{SECOND}")["state"]["closed"]

    restored = LoopDesk()
    after = Plugin(ToolApi(Gateway(), LoopOpening(restored), restored))
    try:
        move = await after.call("aifa_move", ctx, {**MOVE, "doc_id": SECOND})
        assert first_line(move) == "ОТКАЗ цикл_закрыт"
        assert first_line(await after.call("aifa_finish", ctx, {"action": "give_up"})) == "ПРИНЯТО"
        assert first_line(await after.call("aifa_finish", ctx, {"action": "give_up"})) == "ОТКАЗ цикл_закрыт"
        await run.end()
        report_frames = [ev for ev in await run.events() if ev["type"] == "turn_documents"]
        assert report_frames == [{"type": "turn_documents", "document_ids": [DOC, SECOND]}]
    finally:
        await after.close()
