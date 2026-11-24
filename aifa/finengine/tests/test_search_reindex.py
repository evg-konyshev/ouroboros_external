from __future__ import annotations

import os
import sys
import uuid
from pathlib import Path
from unittest.mock import Mock

import httpx
import pytest
from gigachat.exceptions import ResponseError
from opensearchpy.helpers import BulkIndexError

from finengine.config import settings
from finengine.models.document import DocumentMeta
from finengine.search import reindex as reindex_cli
from finengine.search.client import SearchIndex
from finengine.search.indexing import DocumentIndexer
from finengine.search.reindex import ProjectionReindex
from finengine.store.blobs import save_document_text
from finengine.store.documents import SqlDocumentStore

pytestmark = pytest.mark.usefixtures("бд_хранилищ")


class FakeEmbeddings:
    provider = "gigachat"
    model = "Embeddings"

    def embed(self, texts: list[str]) -> list[list[float]]:
        return [[1.0, 2.0, 3.0] for _ in texts]


@pytest.fixture
def store(бд: str) -> SqlDocumentStore:
    return SqlDocumentStore(бд)


@pytest.fixture
def local_blobs(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(settings, "data_dir", tmp_path)
    monkeypatch.setattr(settings, "docs_dir", tmp_path / "docs")
    monkeypatch.setattr(settings, "s3_endpoint", "")


@pytest.fixture
def projection():
    url = os.environ.get("FINENGINE_TEST_OPENSEARCH_URL", "")
    if not url:
        pytest.skip("нужен одноразовый OpenSearch")
    alias = f"test-reindex-{uuid.uuid4().hex}"
    index = SearchIndex(url, alias=alias)
    try:
        yield index
    finally:
        current = index.resolve_alias()
        if current:
            index.delete_index(current)


class OldIndexWriter(SearchIndex):
    def __init__(self, url: str, alias: str, store: SqlDocumentStore) -> None:
        super().__init__(url, alias=alias)
        self.store = store
        self.old_name = ""

    def switch_alias(self, name: str) -> None:
        super().switch_alias(name)
        if self.old_name and self.old_name != name:
            _publish(self.store, "racer", "indexed")
            DocumentIndexer(self.store, self, FakeEmbeddings()).index_document(
                "racer", index_name=self.old_name
            )


class UploadDuringCardScan(SearchIndex):
    store: SqlDocumentStore

    def card_revisions(self, index_name: str | None = None) -> dict[str, str]:
        _publish(self.store, "just-uploaded", "indexed")
        DocumentIndexer(self.store, self, FakeEmbeddings()).index_document(
            "just-uploaded", index_name
        )
        return super().card_revisions(index_name)


class ReclassifiedBeforeSwap(SearchIndex):
    store: SqlDocumentStore

    def switch_alias(self, name: str) -> None:
        current = self.resolve_alias()
        if current is not None:
            meta = self.store.get_document("classification")
            assert meta is not None
            meta.meta_card = {"issuer": "Новый эмитент"}
            self.store.update_document_meta(meta)
            DocumentIndexer(self.store, self, FakeEmbeddings()).index_document(meta.doc_id, current)
        super().switch_alias(name)


def _publish(store: SqlDocumentStore, doc_id: str, state: str) -> None:
    store.add_document(
        DocumentMeta(
            doc_id=doc_id,
            filename=f"{doc_id}.txt",
            owner_id=7,
            workspace_id="F1",
            meta_card={"issuer": "Сбербанк", "doc_kind": "отчётность", "period": "2024"},
        ),
        [],
    )
    save_document_text(doc_id, f"===PAGE 1===\nСодержимое {doc_id}.")
    store.set_index_state(doc_id, state)


def test_reconcile_restores_three_documents_and_second_run_writes_nothing(
    store: SqlDocumentStore, projection: SearchIndex, local_blobs: None
) -> None:
    for index in range(3):
        _publish(store, f"outage-{index}", "failed:outage")
    reindex = ProjectionReindex(store, projection, FakeEmbeddings())
    reindex.init()

    first = reindex.reconcile()
    second = reindex.reconcile()

    assert first.indexed == 3
    assert first.removed == 0
    assert second.indexed == 0
    assert second.removed == 0


class InvalidChunkVector(FakeEmbeddings):
    def embed(self, texts: list[str]) -> list[list[float]]:
        return [[1.0, 2.0] if text == "Битый вектор." else [1.0, 2.0, 3.0] for text in texts]


def test_reconcile_repairs_a_document_after_a_partial_bulk_failure(
    store: SqlDocumentStore, projection: SearchIndex, local_blobs: None
) -> None:
    _publish(store, "partial", "failed:outage")
    save_document_text("partial", "===PAGE 1===\nГотовый чанк.\n===PAGE 2===\nБитый вектор.")
    reindex = ProjectionReindex(store, projection, FakeEmbeddings())
    reindex.init()

    with pytest.raises(BulkIndexError):
        DocumentIndexer(store, projection, InvalidChunkVector()).index_document("partial")

    assert reindex.reconcile().indexed == 1
    records = projection.document_records("partial")
    assert sorted(record["page"] for record in records if record["record_kind"] == "chunk") == [
        1,
        2,
    ]
    assert reindex.reconcile().indexed == 0


def test_reconcile_removes_an_orphan_card(
    store: SqlDocumentStore, projection: SearchIndex, local_blobs: None
) -> None:
    _publish(store, "orphan", "indexed")
    reindex = ProjectionReindex(store, projection, FakeEmbeddings())
    reindex.init()
    reindex.reconcile()
    store.remove_document("orphan")

    stats = reindex.reconcile()

    assert stats.removed == 1
    assert projection.card_ids() == set()


def test_rebuild_reconciles_a_document_written_to_the_old_index(
    store: SqlDocumentStore, projection: SearchIndex, local_blobs: None
) -> None:
    url = os.environ["FINENGINE_TEST_OPENSEARCH_URL"]
    alias = f"test-rebuild-{uuid.uuid4().hex}"
    index = OldIndexWriter(url, alias, store)
    reindex = ProjectionReindex(store, index, FakeEmbeddings())
    reindex.init()
    index.old_name = index.resolve_alias() or ""
    try:
        stats = reindex.rebuild()

        assert stats.indexed == 1
        assert index.card_ids() == {"racer"}
    finally:
        current = index.resolve_alias()
        if current:
            index.delete_index(current)


def test_empty_index_does_not_trust_indexed_state(
    store: SqlDocumentStore, projection: SearchIndex, local_blobs: None
) -> None:
    _publish(store, "stale-state", "indexed")
    reindex = ProjectionReindex(store, projection, FakeEmbeddings())
    reindex.init()

    stats = reindex.reconcile()

    assert stats.indexed == 1
    assert projection.card_ids() == {"stale-state"}


class FailingEmbeddings(FakeEmbeddings):
    def embed(self, texts: list[str]) -> list[list[float]]:
        if any("bad" in text for text in texts):
            raise RuntimeError("provider failed")
        return super().embed(texts)


def test_reconcile_continues_after_one_document_fails(
    store: SqlDocumentStore, projection: SearchIndex, local_blobs: None
) -> None:
    _publish(store, "good", "failed:outage")
    _publish(store, "bad", "failed:outage")
    reindex = ProjectionReindex(store, projection, FailingEmbeddings())
    reindex.init()

    stats = reindex.reconcile()

    assert stats.indexed == 1
    assert stats.failed == 1
    assert projection.card_ids() == {"good"}
    assert store.get_index_state("bad").startswith("failed:")


def test_rebuild_preserves_the_current_index_when_staging_fails(
    store: SqlDocumentStore, projection: SearchIndex, local_blobs: None
) -> None:
    _publish(store, "bad", "indexed")
    reindex = ProjectionReindex(store, projection, FakeEmbeddings())
    old = reindex.init()
    reindex.reconcile()

    stats = ProjectionReindex(store, projection, FailingEmbeddings()).rebuild()

    assert stats.failed == 1
    assert projection.resolve_alias() == old
    assert projection.card_ids() == {"bad"}


def test_rebuild_refreshes_a_card_classified_during_the_swap(
    store: SqlDocumentStore, projection: SearchIndex, local_blobs: None
) -> None:
    _publish(store, "classification", "indexed")
    switching = ReclassifiedBeforeSwap(
        os.environ["FINENGINE_TEST_OPENSEARCH_URL"], projection.alias
    )
    switching.store = store
    reindex = ProjectionReindex(store, switching, FakeEmbeddings())
    reindex.init()

    assert reindex.rebuild().failed == 0

    cards = [
        record
        for record in projection.document_records("classification")
        if record["record_kind"] == "document"
    ]
    assert [card["company"] for card in cards] == ["Новый эмитент"]
    assert reindex.reconcile().indexed == 0


def test_reconcile_retries_a_failed_document_even_with_an_old_card(
    store: SqlDocumentStore, projection: SearchIndex, local_blobs: None
) -> None:
    _publish(store, "prior-failure", "failed:outage")
    reindex = ProjectionReindex(store, projection, FakeEmbeddings())
    reindex.init()
    projection.bulk(
        [
            {
                "id": "prior-failure:card",
                "source": {
                    "record_kind": "document",
                    "document_id": "prior-failure",
                    "workspace_id": "F1",
                },
            }
        ]
    )

    assert reindex.reconcile().indexed == 1
    assert len(projection.document_records("prior-failure")) == 2
    assert reindex.reconcile().indexed == 0


def test_reconcile_does_not_remove_a_document_published_during_the_scan(
    store: SqlDocumentStore, projection: SearchIndex, local_blobs: None
) -> None:
    ProjectionReindex(store, projection, FakeEmbeddings()).init()
    scanning = UploadDuringCardScan(os.environ["FINENGINE_TEST_OPENSEARCH_URL"], projection.alias)
    scanning.store = store

    stats = ProjectionReindex(store, scanning, FakeEmbeddings()).reconcile()

    assert stats.removed == 0
    assert projection.card_ids() == {"just-uploaded"}


def test_init_failure_reports_a_safe_reason_without_traceback(
    store: SqlDocumentStore,
    projection: SearchIndex,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    error = ResponseError(
        "https://example.test/embeddings",
        400,
        b"test-body",
        httpx.Headers(
            {
                "X-Example": "test-header",
            }
        ),
    )
    monkeypatch.setattr(sys, "argv", ["reindex", "--init"])
    monkeypatch.setattr(reindex_cli, "get_store", lambda: store)
    monkeypatch.setattr(reindex_cli, "SearchIndex", lambda url: projection)
    monkeypatch.setattr(reindex_cli.EmbeddingGateway, "embed", Mock(side_effect=error))

    with pytest.raises(SystemExit) as stopped:
        reindex_cli.main()

    assert stopped.value.code == 1
    assert capsys.readouterr().err == "Поисковый индекс: ResponseError (HTTP 400)\n"
