from __future__ import annotations

import asyncio
from decimal import Decimal

from finengine.assistant import ouro_view
from finengine.assistant.tool_call import BoundCall
from finengine.assistant.turn_record import TurnRecord
from finengine.config import settings
from finengine.contracts import CalculatedMetric, ExtractedMetric, UserProvidedMetric
from finengine.ingestion.document_store import get_store
from finengine.models.document import DocumentMeta
from finengine.search.card import build_card
from finengine.skills.atoms.read_override import metric_evidence_id, metric_value_unit_period
from finengine.skills.cross_document import Document, Exclusion, Fact, Preset, compare
from finengine.skills.registry_composition import RegistryCompositionError, RegistrySelection
from finengine.store.cases import get_case_store


class CompareTool:
    name = "aifa_compare"
    root_only = False

    async def run(self, call: BoundCall) -> str:
        name = call.args.get("preset")
        try:
            preset = Preset(name if isinstance(name, str) else "")
        except ValueError:
            return ouro_view.refusal(
                ouro_view.Refusal.BAD_ARGUMENTS, "Укажи один из режимов: " + ", ".join(Preset) + "."
            )
        ids = call.args.get("artifact_ids")
        if (
            not isinstance(ids, list)
            or not 2 <= len(ids) <= ouro_view.STATE_BATCH
            or any(not isinstance(item, str) or not item.strip() for item in ids)
        ):
            return ouro_view.refusal(
                ouro_view.Refusal.BAD_ARGUMENTS, "artifact_ids — от 2 до 20 id фактов."
            )
        return await asyncio.to_thread(_answer, call.record, preset, set(ids))


def _document(meta: DocumentMeta) -> Document:
    card = build_card(meta, "")
    standard, company_type = "", ""
    if meta.profile_id:
        try:
            profile = RegistrySelection.from_profile_id(meta.profile_id)
        except RegistryCompositionError:
            pass
        else:
            standard, company_type = profile.standard, profile.company_type
    return Document(
        meta.doc_id,
        meta.filename,
        card.company_key,
        card.company,
        card.doc_kind,
        standard,
        company_type,
    )


def _answer(record: TurnRecord, preset: Preset, ids: set[str]) -> str:
    case_store = get_case_store()
    owner, case_id = record.user_id, record.case_id
    active = {
        row.document_version: row.document_id for row in case_store.active_documents(owner, case_id)
    }
    doc_store = get_store()
    documents = {
        doc_id: _document(meta)
        for doc_id in sorted(set(active.values()) | set(record.doc_ids))
        if (meta := doc_store.get_document(doc_id)) is not None
    }
    # Overrides are in the Case audit trail even before any node consumes them.
    overrides = tuple(case_store.active_overrides(owner, case_id).values())
    rows = case_store.list_artifacts(owner, case_id, valid_only=False)
    named = [(value, True, None) for value in overrides]
    named.extend(
        (value, row["valid"], row["document_version"]) for row in rows for value in row["payloads"]
    )
    found: dict[str, Fact] = {}
    excluded: dict[str, Exclusion] = {}
    for value, valid, row_version in named:
        if not isinstance(value, (ExtractedMetric, CalculatedMetric, UserProvidedMetric)):
            continue
        fact_id = metric_evidence_id(value)
        if fact_id not in ids or fact_id in found:
            continue
        try:
            amount, unit, period, basis, version = metric_value_unit_period(value)
        except ValueError:
            continue
        document = documents.get(active.get(version or row_version or "", ""))
        if document is None:
            excluded[fact_id] = Exclusion(
                fact_id, "версия документа не активна в Case или документ удалён"
            )
        elif valid:
            number = Decimal(str(amount))
            if number.is_finite() and unit:
                found[fact_id] = Fact(
                    fact_id, document, value.concept, period, number, unit, basis or ""
                )
    missing = tuple(sorted(ids - found.keys() - excluded.keys()))
    rejected = tuple(excluded[fact_id] for fact_id in sorted(excluded.keys() - found.keys()))
    if len(found) < 2:
        return ouro_view.compare_no_inputs(missing, rejected, limit=settings.ouroboros_result_limit)
    coverage = (documents[doc_id] for doc_id in record.doc_ids if doc_id in documents)
    result = compare(preset, found.values(), coverage)
    return ouro_view.compared(result, missing, rejected, limit=settings.ouroboros_result_limit)
