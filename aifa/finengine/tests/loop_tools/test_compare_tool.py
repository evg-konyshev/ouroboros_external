from __future__ import annotations

import asyncio

from loop_support import Gateway
from sqlalchemy import event
from test_ephemeral_in_graph import CASE
from tool_harness import Plugin, ctx_of, first_line, open_run

from finengine.assistant import compare_tool
from finengine.assistant.loop_desk import LoopDesk
from finengine.assistant.tool_api import ToolApi
from finengine.config import settings
from finengine.contracts import DynamicArtifact, ExtractedMetric, NodeKey, ScalarFact
from finengine.models.document import DocumentMeta
from finengine.store.cases import SqlCaseStore
from finengine.store.documents import SqlDocumentStore


OWNER = 7


def _register(base, case, doc_id, version):
    base.register_document(
        owner_id=OWNER,
        case_id=case,
        logical_name=doc_id,
        document_id=doc_id,
        document_version=version,
    )


def _metric(base, evidence_id, version, *, value, case=CASE):
    metric = ExtractedMetric(
        concept="NET_INCOME",
        period_ref="FY 2024",
        scalar_fact=ScalarFact(
            evidence_id=evidence_id,
            produced_by_atom="read.value",
            produced_by_atom_version="1",
            document_version=version,
            source_ref="source",
            period_ref="FY 2024",
            value=value,
            unit="млн. руб.",
            basis="consolidated",
        ),
    )
    _put(base, case, version, evidence_id, "ExtractedMetric", metric)


def _put(base, case, version, evidence_id, output_type, value):
    base.store.put_artifact(
        OWNER,
        case,
        node_key=NodeKey(atom_name="read.value", atom_version="1", args_fingerprint=evidence_id),
        output_type=output_type,
        payloads=(value,),
        concept="NET_INCOME",
        entity_ref=None,
        period_ref=None,
        basis=None,
        document_version=version,
        policy_version="test",
    )


def _documents(tmp_path, monkeypatch, base):
    store = SqlDocumentStore(f"sqlite:///{tmp_path / 'comparison-docs.db'}")
    for doc_id, company, profile in (
        ("s", "Сбербанк", "ifrs_bank_ru"),
        ("v", "ВТБ", "rsbu_bank_ru"),
    ):
        store.add_document(
            DocumentMeta(
                doc_id=doc_id,
                filename=f"{company}.pdf",
                company_name="Не метакарточка",
                meta_card={"issuer": company},
                profile_id=profile,
                owner_id=OWNER,
            ),
            [],
        )
        store.set_profile_id(doc_id, profile)
    monkeypatch.setattr(compare_tool, "get_store", lambda: store)
    monkeypatch.setattr(compare_tool, "get_case_store", lambda: base.store)
    return store


async def test_presets_read_only_named_metric_facts_from_this_case(base, tmp_path, monkeypatch):
    _documents(tmp_path, monkeypatch, base)
    base.ensure_case(owner_id=OWNER, case_id=CASE)
    outside = "another-case"
    base.ensure_case(owner_id=OWNER, case_id=outside)
    _register(base, outside, "v", "outside")
    _metric(base, "foreign", "outside", value=700, case=outside)
    _register(base, CASE, "s", "s1")
    _metric(base, "e1", "s1", value=100)
    non_metric = DynamicArtifact(
        output_type="dyn:note",
        payload={},
        origin=["generated"],
        producer="test@1",
        input_evidence_ids=[],
    )
    _put(base, CASE, "s1", non_metric.evidence_id, non_metric.output_type, non_metric)
    run = open_run(user_id=OWNER, case_id=CASE, doc_ids=("s", "v"))
    plugin = Plugin(ToolApi(Gateway(), None))
    ctx = ctx_of(run.run_id)
    try:
        reply = await plugin.call(
            "aifa_compare",
            ctx,
            {
                "preset": "peer_comparison",
                "artifact_ids": ["e1", "foreign", non_metric.evidence_id],
            },
        )
        assert first_line(reply) == "ОТКАЗ нет_входов"
        assert "Не найден | foreign" in reply.text
        assert f"Не найден | {non_metric.evidence_id}" in reply.text
        assert "700" not in reply.text
        bad = await plugin.call(
            "aifa_compare", ctx, {"preset": "ranking", "artifact_ids": ["e1", "foreign"]}
        )
        assert first_line(bad) == "ОТКАЗ неверные_аргументы"
    finally:
        await plugin.close()
        await run.end()


async def test_case_comparison_uses_meta_and_profile_outside_queue_even_before_open(
    base, tmp_path, monkeypatch
):
    _documents(tmp_path, monkeypatch, base)
    base.ensure_case(owner_id=OWNER, case_id=CASE)
    for doc_id, evidence, value in (("s", "e1", 100), ("v", "e2", 90)):
        _register(base, CASE, doc_id, doc_id)
        _metric(base, evidence, doc_id, value=value)
    _register(base, CASE, "s", "s-replaced")
    _metric(base, "e3", "s-replaced", value=105)
    run = open_run(user_id=OWNER, case_id=CASE, doc_ids=("s", "v"))
    desk = LoopDesk()
    plugin = Plugin(ToolApi(Gateway(), None, desk))
    ctx = ctx_of(run.run_id)
    args = {"preset": "peer_comparison", "artifact_ids": ["e1", "e2", "e3"]}
    try:
        async with desk.queue_of(run.run_id):
            reply = await asyncio.wait_for(plugin.call("aifa_compare", ctx, args), timeout=2)
        assert first_line(reply) == "ГОТОВО"
        assert "Исключён | e1 | версия документа не активна" in reply.text
        assert "разные стандарты" in reply.text
        assert "Сбербанк" in reply.text and "Не метакарточка" not in reply.text
        assert "105 | RUB млн | [e3]" in reply.text
        reordered = await plugin.call(
            "aifa_compare", ctx, {**args, "artifact_ids": ["e3", "e2", "e1"]}
        )
        assert reordered.text == reply.text

        monkeypatch.setattr(settings, "ouroboros_result_limit", 180)
        short = await plugin.call("aifa_compare", ctx, args)
        assert len(short.text) <= 180
        assert "Не показано строк:" in short.text and short.text.startswith("ГОТОВО\n")
    finally:
        await plugin.close()
        await run.end()


async def test_a_shared_content_version_is_not_arbitrarily_attributed(base, tmp_path, monkeypatch):
    _documents(tmp_path, monkeypatch, base)
    base.ensure_case(owner_id=OWNER, case_id=CASE)
    for doc_id in ("s", "v"):
        _register(base, CASE, doc_id, "same-content")
    _metric(base, "shared", "same-content", value=100)
    run = open_run(user_id=OWNER, case_id=CASE, doc_ids=("s", "v"))
    plugin = Plugin(ToolApi(Gateway(), None))
    try:
        reply = await plugin.call(
            "aifa_compare",
            ctx_of(run.run_id),
            {
                "preset": "peer_comparison",
                "artifact_ids": ["shared", "absent"],
            },
        )
        assert first_line(reply) == "ОТКАЗ нет_входов"
        assert "shared | версия относится к нескольким документам Case" in reply.text
    finally:
        await plugin.close()
        await run.end()


def test_named_facts_and_versions_share_a_postgres_snapshot(base, бд):
    store = SqlCaseStore(бд)
    base.store = store
    base.ensure_case(owner_id=OWNER, case_id=CASE)
    _register(base, CASE, "s", "s1")
    _metric(base, "e1", "s1", value=100)
    _metric(base, "unrelated", "s1", value=900)
    interrupted = False

    def replace_between_reads(conn, cursor, statement, parameters, context, executemany):
        nonlocal interrupted
        if not interrupted and statement.lstrip().startswith(
            "SELECT analysis_case_artifact.payloads"
        ):
            interrupted = True
            _register(base, CASE, "s", "s2")
            _metric(base, "e2", "s2", value=110)

    event.listen(store._engine, "after_cursor_execute", replace_between_reads)
    try:
        facts, active = store.comparison_inputs(OWNER, CASE, {"e1", "e2"})
        assert interrupted
        assert [
            (fact.scalar_fact.evidence_id, valid, version) for fact, valid, version in facts
        ] == [
            ("e1", True, "s1"),
        ]
        assert active == [("s1", "s")]
        current, active = store.comparison_inputs(OWNER, CASE, {"e1", "e2"})
        assert {fact.scalar_fact.evidence_id for fact, _, _ in current} == {"e1", "e2"}
        assert active == [("s2", "s")]
    finally:
        event.remove(store._engine, "after_cursor_execute", replace_between_reads)
