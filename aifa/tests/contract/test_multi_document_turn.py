"""C3.12–C3.15 through Ouroboros, the real report registry and the run journal."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import httpx
import pipeline_fixture as fixture
import pytest
from engine_host import running_engine
from finengine.contracts import ExtractedMetric, NodeKey, ScalarFact
from finengine.contracts.case import CaseIdentity
from finengine.ingestion.document_store import get_store
from finengine.models.document import DocumentMeta
from finengine.store.blobs import save_document_text
from finengine.store.cases import get_case_store
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
    """C3.12–C3.15 through the real document registry, tools and stream."""
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


async def test_the_planner_compares_existing_case_facts_before_opening_a_report(
    stand: ContractStand,
    stub: StubModelAdmin,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """C3.16 reads named prior Case facts through the real plugin without opening."""
    args = {"preset": "peer_comparison", "artifact_ids": ["e-sber", "e-vtb"]}
    stub.use_scenario(
        {
            "name": "compare-existing-case-facts",
            "rules": [
                {"id": "title", "when": {"model": "stub-light"}, "reply": {"text": "Сравнение"}},
                _call(1, "aifa_compare", args),
                {
                    "id": "answer",
                    "when": {"model": "stub-main", "round": 2},
                    "reply": {"text": "Готово."},
                },
            ],
        }
    )
    with running_engine(stand, tmp_path, monkeypatch, real_documents=True) as engine:
        owner, case_id = fixture.OWNER, fixture.PROJECT
        cases = get_case_store()
        cases.ensure_case(CaseIdentity(case_id=case_id, owner_id=str(owner)))
        docs = get_store()
        for doc_id, company, amount in (("sber", "Сбербанк", 100), ("vtb", "ВТБ", 90)):
            version = f"version-{doc_id}"
            docs.add_document(
                DocumentMeta(
                    doc_id=doc_id,
                    filename=f"{company}.pdf",
                    owner_id=owner,
                    meta_card={"issuer": company},
                ),
                [],
            )
            docs.set_profile_id(doc_id, "ifrs_bank_ru")
            cases.register_document(
                owner,
                case_id,
                logical_name=doc_id,
                document_id=doc_id,
                document_version=version,
            )
            fact = ExtractedMetric(
                concept="NET_INCOME",
                period_ref="FY 2024",
                scalar_fact=ScalarFact(
                    evidence_id=f"e-{doc_id}",
                    produced_by_atom="read.value",
                    produced_by_atom_version="1",
                    source_ref="source",
                    period_ref="FY 2024",
                    document_version=version,
                    value=amount,
                    unit="млн. руб.",
                    basis="consolidated",
                ),
            )
            cases.put_artifact(
                owner,
                case_id,
                node_key=NodeKey(
                    atom_name="read.value", atom_version="1", args_fingerprint=version
                ),
                output_type="ExtractedMetric",
                payloads=(fact,),
                concept="NET_INCOME",
                entity_ref=None,
                period_ref="FY 2024",
                basis="consolidated",
                document_version=version,
                policy_version="test",
            )
        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.post(
                f"{engine}/chat/turns",
                json={"message": "Сравни факты прошлого хода", "project_id": case_id},
            )
        assert response.status_code == 200, response.text
        events = await screen_events(engine, response.json()["run_id"])

    assert events[-1]["status"] == "succeeded"
    asked = [entry for entry in stub.journal() if entry["model"] == "stub-main"]
    assert [entry["rule"] for entry in asked] == ["1-aifa_compare", "answer"]
    result = asked[-1]["last_tool_result"]
    assert result.startswith("ГОТОВО\n")
    assert "Сбербанк | Сбербанк.pdf" in result and "100 | RUB млн | [e-sber]" in result
    assert "ВТБ | ВТБ.pdf" in result and "90 | RUB млн | [e-vtb]" in result
    assert "Место: Сбербанк | 1 | [e-sber], [e-vtb]" in result
