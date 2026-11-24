from __future__ import annotations

import re
from dataclasses import dataclass

from finengine.models.document import DocumentMeta
from finengine.search.company_key import company_key

_HEADING = re.compile(r"(?m)^#\s+(.+?)\s*$")
_TABLE_CAPTION = re.compile(r"^(?:таблица|table)\b", re.IGNORECASE)


@dataclass(frozen=True)
class DocumentCard:
    document_id: str
    workspace_id: str
    title: str
    company: str
    company_key: str
    doc_kind: str
    period: str
    language: str
    description: str
    headings: tuple[str, ...]


def _headings(text: str) -> tuple[str, ...]:
    return tuple(
        heading.strip()
        for heading in _HEADING.findall(text)
        if not _TABLE_CAPTION.match(heading.strip())
    )[:20]


def build_card(meta: DocumentMeta, text: str) -> DocumentCard:
    card = meta.meta_card or {}
    company = str(card.get("issuer") or meta.company_name or "")
    doc_kind = str(card.get("doc_kind") or meta.doc_type.value or "")
    period = str(card.get("period") or meta.period or "")
    aliases = card.get("issuer_aliases") or []
    description = " · ".join(part for part in (doc_kind, company, period) if part)
    return DocumentCard(
        document_id=meta.doc_id,
        workspace_id=meta.workspace_id,
        title=meta.filename,
        company=company,
        company_key=company_key(company, aliases),
        doc_kind=doc_kind,
        period=period,
        language=meta.language,
        description=description,
        headings=_headings(text),
    )
