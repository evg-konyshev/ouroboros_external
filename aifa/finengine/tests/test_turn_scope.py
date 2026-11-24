"""C3.12: POST /chat/turns freezes the document scope in the turn record."""

from __future__ import annotations

import httpx
import pytest

from finengine.api.main import _chat_turn_docs, app, get_current_user
from finengine.assistant import composition
from finengine.assistant.turn_record import recall
from finengine.auth.db import User
from finengine.config import settings
from finengine.ingestion.document_store import get_store, reset_store
from finengine.models.document import DocumentMeta
from finengine.models.query import QueryRequest
from finengine.orchestrator.agent import get_agent
from finengine.skills import run_logger
from finengine.store.blobs import save_document_text
from finengine.store.cache import get_cache
from finengine.store.dialogs import get_dialog_store


class _Gateway:
    async def events(self, request, *, on_root):
        yield {"type": "text_delta", "text": "Ответ"}


async def _caller() -> User:
    return User(id=7, username="test", is_admin=False)


@pytest.fixture
async def client(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "data_dir", tmp_path)
    monkeypatch.setattr(settings, "docs_dir", tmp_path)
    monkeypatch.setattr(settings, "state_database_url", "")
    monkeypatch.setattr(settings, "journals_database_url", "")
    monkeypatch.setattr(settings, "dialogs_database_url", "")
    monkeypatch.setattr(settings, "redis_url", "")
    monkeypatch.setattr(run_logger, "_CHAT_LOG_PATH", tmp_path / "chat_runs.jsonl")
    monkeypatch.setattr(composition, "turn_gateway", _Gateway)
    monkeypatch.setitem(app.dependency_overrides, get_current_user, _caller)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://engine") as client:
        yield client


async def test_unknown_scope_rejected_before_a_turn_is_remembered(client):
    cache = get_cache()
    before = set(cache.keys("ouro_turn:"))

    response = await client.post("/chat/turns", json={"message": "Выручка", "scope": "everything"})

    assert response.status_code == 422, response.text
    assert response.json()["detail"][0]["loc"] == ["body", "scope"]
    assert all(recall(key.removeprefix("ouro_turn:")) is None for key in set(cache.keys("ouro_turn:")) - before)


@pytest.fixture
def documents(client, бд, monkeypatch):
    monkeypatch.setattr(settings, "documents_database_url", бд)
    reset_store()
    store = get_store()
    for doc_id, workspace_id, owner_id in [
        ("A", "F1", 7), ("B", "F1", 7), ("C", "F1", 7), ("D", "F2", 7),
        ("default", "owner:7", 7), ("stray", "P", 7), ("foreign", "F9", 8),
    ]:
        store.add_document(
            DocumentMeta(doc_id=doc_id, filename=f"{doc_id}.txt", workspace_id=workspace_id, owner_id=owner_id), []
        )
    get_dialog_store().save(7, {"projects": [], "folders": [{"id": f} for f in ("F1", "F2", "F9")]})
    save_document_text("A", "Первый отчёт")
    save_document_text("B", "Второй отчёт")
    yield store
    reset_store()


@pytest.mark.parametrize(
    ("request_body", "workspace_id", "doc_ids"),
    [
        ({"project_id": "F1", "active_doc_id": "D"}, "F1", ("A", "B", "C")),
        ({"project_id": "F1", "doc_ids": ["B", "D", "foreign"], "active_doc_id": "A"}, "F1", ("B",)),
        ({"project_id": "F2"}, "F2", ("D",)),
        ({"project_id": "F9"}, "owner:7", ("default",)),
        ({"project_id": "P"}, "owner:7", ("default",)),
        ({}, "owner:7", ("default",)),
    ],
)
async def test_workspace_scope(client, documents, request_body, workspace_id, doc_ids):
    response = await client.post("/chat/turns", json={"message": "Выручка", "scope": "all_workspace", **request_body})

    assert response.status_code == 200, response.text
    run_id = response.json()["run_id"]
    record = recall(run_id)
    assert record is not None
    assert record.doc_ids == doc_ids
    assert (record.scope, record.workspace_id) == ("all_workspace", workspace_id)
    assert record.active_doc_id == ""
    documents.add_document(DocumentMeta(doc_id="later", filename="later.txt", workspace_id=workspace_id, owner_id=7), [])
    assert recall(run_id) == record


async def test_conversation_scope_keeps_only_its_accessible_documents(client, documents):
    documents.remove_document("C")
    response = await client.post(
        "/chat/turns",
        json={"message": "Выручка", "project_id": "F1", "doc_ids": ["A", "C", "foreign"], "active_doc_id": "B"},
    )
    empty = await client.post("/chat/turns", json={"message": "Выручка", "project_id": "F1"})

    original = recall(response.json()["run_id"])
    empty_record = recall(empty.json()["run_id"])
    assert original is not None and empty_record is not None
    assert (original.scope, original.workspace_id, original.doc_ids, original.active_doc_id) == (
        "conversation", "F1", ("A", "B"), "B",
    )
    assert empty_record.doc_ids == ()


async def test_pinned_tool_ignores_workspace_scope(client, documents):
    request = QueryRequest(message="Выручка", project_id="F1", scope="all_workspace", pinned_tool="fe_calc", doc_ids=["D"])

    assert _chat_turn_docs(request, await _caller(), get_agent()) == ["D"]


async def test_conversation_followup_inherits_session_documents(client, documents):
    agent = get_agent()
    session = agent.get_or_create_session(auth_user_id=7)
    session.doc_ids = ["B", "foreign"]
    try:
        response = await client.post("/chat/turns", json={"message": "Ещё выручка", "session_id": session.session_id})
    finally:
        agent.sessions.pop(session.session_id)

    assert response.status_code == 200, response.text
    record = recall(response.json()["run_id"])
    assert record is not None and record.doc_ids == ("B",)


async def test_recall_accepts_turn_records_from_before_workspace_scope(client):
    response = await client.post("/chat/turns", json={"message": "Выручка"})
    run_id = response.json()["run_id"]
    cache = get_cache()
    key = f"ouro_turn:{run_id}"
    legacy = {k: v for k, v in cache.get(key).items() if k not in {"scope", "workspace_id"}}
    cache.set(key, legacy, 60)

    record = recall(run_id)
    assert record is not None
    assert (record.scope, record.workspace_id, record.doc_ids) == ("conversation", "owner:7", ())
