"""C3.12–C3.15 through Ouroboros, the real report registry and the run journal."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import httpx
import pipeline_fixture as fixture
import pytest
from engine_host import running_engine
from test_chat_turn_end_to_end import kind_of, screen_events
from test_loop_end_to_end import TOOL

from stand import ContractStand
from stub_model.client import StubModelAdmin

pytestmark = [pytest.mark.contract, pytest.mark.asyncio]


def _call(round_number: int, name: str, args: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": f"{round_number}-{name}",
        "when": {"model": "stub-main", "round": round_number},
        "reply": {"tool_calls": [{"name": TOOL.format(name=name), "arguments": args}]},
    }


async def test_the_folder_turn_lists_opens_and_finishes_two_reports(
    stand: ContractStand,
    stub: StubModelAdmin,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from finengine.ingestion.document_store import get_store
    from finengine.models.document import DocumentMeta
    from finengine.store.blobs import save_document_text

    docs = ("multidoc-a", "multidoc-b")
    outside = "outside-folder"
    stub.use_scenario(
        {
            "name": "multi-document-turn",
            "rules": [
                {"id": "title", "when": {"model": "stub-light"}, "reply": {"text": "Два отчёта"}},
                _call(1, "aifa_doc", {}),
                _call(2, "aifa_open", {"doc_id": docs[0]}),
                _call(3, "aifa_open", {"doc_id": docs[1]}),
                _call(4, "aifa_state", {}),
                _call(5, "aifa_finish", {"action": "finalize"}),
                {
                    "id": "answer",
                    "when": {"model": "stub-main", "round": 6},
                    "reply": {"text": "Прочитал два отчёта."},
                },
            ],
        }
    )
    with running_engine(stand, tmp_path, monkeypatch, real_documents=True) as engine:
        store = get_store()
        for doc_id in (*docs, outside):
            store.add_document(
                DocumentMeta(
                    doc_id=doc_id,
                    filename=f"{doc_id}.pdf",
                    owner_id=fixture.OWNER,
                    workspace_id="separate" if doc_id == outside else "",
                ),
                [],
            )
            save_document_text(doc_id, fixture.document_text())
        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.post(
                f"{engine}/chat/turns",
                json={"message": "Посмотри два отчёта", "scope": "all_workspace", "doc_ids": []},
            )
        assert response.status_code == 200, response.text
        events = await screen_events(engine, response.json()["run_id"])

    frames = [event["legacy_event"] for event in events if kind_of(event) == "turn_documents"]
    assert frames == [{"type": "turn_documents", "document_ids": list(docs)}]
    moves = [event["legacy_event"] for event in events if kind_of(event) == "dynamic_move"]
    assert [move["action"] for move in moves if move["status"] == "done"] == ["finalize"]
    results = [
        entry["last_tool_result"] for entry in stub.journal() if entry["model"] == "stub-main"
    ]
    assert all(doc in results[1] for doc in docs) and outside not in results[1]
    assert "## Обзор хода" in results[4]
    assert all(f"{doc}.pdf" in results[5] for doc in docs)
