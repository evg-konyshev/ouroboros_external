from __future__ import annotations

import argparse
import json
import uuid
from dataclasses import asdict, dataclass

from finengine.config import settings
from finengine.ingestion.document_store import get_store
from finengine.search.client import SearchIndex, SearchUnavailable
from finengine.search.embeddings import EmbeddingGateway
from finengine.search.index_schema import index_body
from finengine.search.indexing import (
    DocumentIndexer,
    EmbeddingPort,
    document_revision,
    index_failure_reason,
)
from finengine.store.blobs import document_text
from finengine.store.documents import SqlDocumentStore


@dataclass(frozen=True)
class ReconcileStats:
    indexed: int
    removed: int
    failed: int = 0


class ProjectionReindex:
    def __init__(
        self,
        store: SqlDocumentStore,
        index: SearchIndex,
        embeddings: EmbeddingPort,
    ) -> None:
        self.store = store
        self.index = index
        self.embeddings = embeddings

    def _new_name(self) -> str:
        return f"{self.index.alias}-{uuid.uuid4().hex}"

    def _create(self, name: str) -> None:
        vector = self.embeddings.embed(["размерность поискового индекса"])[0]
        self.index.create(
            name,
            index_body(
                dimension=len(vector),
                provider=self.embeddings.provider,
                model=self.embeddings.model,
            ),
        )

    def init(self) -> str:
        current = self.index.resolve_alias()
        if current:
            return current
        name = self._new_name()
        self._create(name)
        self.index.switch_alias(name)
        return name

    def _index_one(
        self,
        indexer: DocumentIndexer,
        doc_id: str,
        index_name: str | None,
    ) -> bool:
        try:
            indexer.index_document(doc_id, index_name)
            return True
        except Exception as error:  # noqa: BLE001 — остальные документы сверка обязана восстановить
            self.store.set_index_state(doc_id, f"failed:{index_failure_reason(error)}")
            return False

    def reconcile(self, index_name: str | None = None) -> ReconcileStats:
        target = index_name or self.index.resolve_alias()
        if target is None:
            raise SearchUnavailable("поисковый индекс ещё не создан")
        ready = {
            meta.doc_id: document_revision(meta, published)
            for meta in self.store.list_documents()
            if (published := document_text(meta.doc_id)) is not None
        }
        cards = self.index.card_revisions(target)
        to_index = {doc_id for doc_id, revision in ready.items() if cards.get(doc_id) != revision}
        to_index.update(ready.keys() & self.store.failed_index_document_ids())
        indexer = DocumentIndexer(self.store, self.index, self.embeddings)
        outcomes = [self._index_one(indexer, doc_id, target) for doc_id in sorted(to_index)]
        removed = 0
        for doc_id in sorted(cards.keys() - ready.keys()):
            if self.store.get_document(doc_id) is None:
                indexer.delete_document(doc_id, target)
                removed += 1
        return ReconcileStats(
            indexed=sum(outcomes),
            removed=removed,
            failed=len(outcomes) - sum(outcomes),
        )

    def rebuild(self) -> ReconcileStats:
        current = self.index.resolve_alias()
        name = self._new_name()
        self._create(name)
        staged = self.reconcile(name)
        if staged.failed:
            self.index.delete_index(name)
            return ReconcileStats(indexed=0, removed=0, failed=staged.failed)
        self.index.switch_alias(name)
        catch_up = self.reconcile(name)
        if current and not catch_up.failed:
            self.index.delete_index(current)
        return ReconcileStats(
            indexed=staged.indexed + catch_up.indexed,
            removed=catch_up.removed,
            failed=catch_up.failed,
        )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument("--init", action="store_true")
    action.add_argument("--rebuild", action="store_true")
    action.add_argument("--reconcile", action="store_true")
    return parser


def main() -> None:
    parser = _parser()
    args = parser.parse_args()
    try:
        reindex = ProjectionReindex(
            get_store(),
            SearchIndex(settings.opensearch_url),
            EmbeddingGateway.from_settings(settings),
        )
        if args.init:
            result: dict | str = reindex.init()
        else:
            result = asdict(reindex.rebuild() if args.rebuild else reindex.reconcile())
    except Exception as error:  # noqa: BLE001 — внешние ошибки могут содержать тело ответа
        parser.exit(1, f"Поисковый индекс: {index_failure_reason(error)}\n")
    print(json.dumps(result, ensure_ascii=False))
    if isinstance(result, dict) and result["failed"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
