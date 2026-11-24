from __future__ import annotations

import logging
from collections.abc import Sequence
from functools import partial
from hashlib import sha256
from typing import Protocol

from finengine.config import settings
from finengine.ingestion.document_store import get_store
from finengine.models.document import DocumentMeta
from finengine.search.card import DocumentCard, build_card
from finengine.search.chunker import SearchChunk, chunk_document
from finengine.search.client import SearchIndex, SearchUnavailable
from finengine.search.embeddings import EmbeddingGateway, IndexRebuildRequired, validate_index_meta
from finengine.store.blobs import document_text
from finengine.store.documents import DocumentChangedDuringIndexing, SqlDocumentStore

log = logging.getLogger(__name__)


class EmbeddingPort(Protocol):
    provider: str
    model: str

    def embed(self, texts: Sequence[str]) -> list[list[float]]: ...


def index_failure_reason(error: Exception) -> str:
    if isinstance(error, IndexRebuildRequired):
        return "нужна пересборка"
    if isinstance(error, DocumentChangedDuringIndexing):
        return "документ изменился при индексации"
    reason = type(error).__name__
    status = getattr(error, "status_code", None)
    return f"{reason} (HTTP {status})" if isinstance(status, int) else reason


def _card_text(card: DocumentCard) -> str:
    return " ".join(
        (
            card.title,
            card.company,
            card.company_key,
            card.doc_kind,
            card.period,
            card.language,
            card.description,
            *card.headings,
        )
    ).strip()


def document_revision(meta: DocumentMeta, text: str) -> str:
    digest = sha256(meta.model_dump_json().encode())
    digest.update(b"\n")
    digest.update(text.encode())
    return digest.hexdigest()


def _card_record(card: DocumentCard, vector: list[float], revision: str) -> dict:
    return {
        "id": f"{card.document_id}:card:{revision}",
        "source": {
            "record_kind": "document",
            "document_id": card.document_id,
            "workspace_id": card.workspace_id,
            "title": card.title,
            "company": card.company,
            "company_key": card.company_key,
            "doc_kind": card.doc_kind,
            "period": card.period,
            "language": card.language,
            "description": card.description,
            "headings": list(card.headings),
            "vector": vector,
        },
    }


def _chunk_record(chunk: SearchChunk, workspace_id: str, vector: list[float]) -> dict:
    return {
        "id": chunk.chunk_id,
        "source": {
            "record_kind": "chunk",
            "document_id": chunk.document_id,
            "workspace_id": workspace_id,
            "chunk_id": chunk.chunk_id,
            "content": chunk.content,
            "page": chunk.page,
            "section_path": chunk.section_path,
            "chunk_type": chunk.chunk_type,
            "vector": vector,
        },
    }


class DocumentIndexer:
    def __init__(
        self,
        store: SqlDocumentStore,
        index: SearchIndex,
        embeddings: EmbeddingPort,
    ) -> None:
        self.store = store
        self.index = index
        self.embeddings = embeddings

    def index_document(self, doc_id: str, index_name: str | None = None) -> int:
        meta = self.store.get_document(doc_id)
        if meta is None:
            return 0
        text = document_text(doc_id)
        if text is None:
            raise ValueError("опубликованный текст документа не найден")
        target = index_name or self.index.resolve_alias()
        if target is None:
            raise SearchUnavailable("поисковый индекс ещё не создан")
        card = build_card(meta, text)
        chunks = chunk_document(
            doc_id, text, require_page_marks=meta.filename.casefold().endswith(".pdf")
        )
        vectors = self.embeddings.embed([_card_text(card), *(chunk.content for chunk in chunks)])
        if not vectors:
            raise ValueError("эмбеддинг карточки не получен")
        dimension = len(vectors[0])
        validate_index_meta(
            self.index.index_meta(target),
            provider=self.embeddings.provider,
            model=self.embeddings.model,
            dimension=dimension,
        )
        records = [
            _chunk_record(chunk, meta.workspace_id, vector)
            for chunk, vector in zip(chunks, vectors[1:], strict=True)
        ]
        card_record = _card_record(card, vectors[0], document_revision(meta, text))
        publish = partial(self._replace_records, doc_id, target, records, card_record, text)
        return len(chunks) if self.store.publish_search_projection(meta, publish) else 0

    def _replace_records(
        self, doc_id: str, target: str, chunks: list[dict], card: dict, indexed_text: str
    ) -> None:
        if document_text(doc_id) != indexed_text:
            raise DocumentChangedDuringIndexing
        self.index.delete_document(doc_id, target)
        self.index.bulk(chunks, target)
        # Наличие карточки означает, что предыдущая запись чанков завершилась.
        self.index.bulk([card], target)

    def delete_document(self, doc_id: str, index_name: str | None = None) -> None:
        self.index.delete_document(doc_id, index_name)


def index_document(doc_id: str, index_name: str | None = None) -> int:
    store = get_store()
    try:
        indexer = DocumentIndexer(
            store,
            SearchIndex(settings.opensearch_url),
            EmbeddingGateway.from_settings(settings),
        )
        return indexer.index_document(doc_id, index_name)
    except Exception as error:  # noqa: BLE001 — поисковая проекция не отменяет загрузку
        reason = index_failure_reason(error)
        store.set_index_state(doc_id, f"failed:{reason}")
        log.warning("индексация документа %s не выполнена: %s", doc_id, reason)
        return 0


def delete_document(doc_id: str, index_name: str | None = None) -> None:
    try:
        SearchIndex(settings.opensearch_url).delete_document(doc_id, index_name)
    except Exception as error:  # noqa: BLE001 — реестр уже удалён, сверка уберёт осиротевшие записи
        log.warning(
            "удаление поисковых записей %s не выполнено: %s", doc_id, index_failure_reason(error)
        )
