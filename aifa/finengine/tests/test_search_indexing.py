from __future__ import annotations

import os
import uuid
from collections.abc import Callable
from pathlib import Path
from unittest.mock import Mock

import fitz
import httpx
import pytest
from fastapi.testclient import TestClient
from gigachat.exceptions import ResponseError
from sqlalchemy import inspect, text

import finengine.api.main as api
from finengine.auth.db import User
from finengine.config import settings
from finengine.models.document import DocumentMeta
from finengine.search import indexing
from finengine.search.client import SearchIndex
from finengine.search.chunker import chunk_document
from finengine.search.index_schema import index_body
from finengine.search.indexing import DocumentIndexer
from finengine.store.blobs import document_text, document_text_exists, save_document_text
from finengine.store.documents import DocumentChangedDuringIndexing, SqlDocumentStore
from finengine.store.engine import движок

pytestmark = pytest.mark.usefixtures("бд_хранилищ")


class FakeEmbeddings:
    provider = "gigachat"
    model = "Embeddings"

    def __init__(self, on_embed: Callable[[], None] | None = None) -> None:
        self.on_embed = on_embed

    def embed(self, texts: list[str]) -> list[list[float]]:
        if self.on_embed:
            self.on_embed()
        return [[float(len(text) % 7), 1.0, 2.0] for text in texts]


@pytest.fixture
def store(бд: str) -> SqlDocumentStore:
    return SqlDocumentStore(бд)


@pytest.fixture
def local_blobs(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(settings, "data_dir", tmp_path)
    monkeypatch.setattr(settings, "docs_dir", tmp_path / "docs")
    monkeypatch.setattr(settings, "s3_endpoint", "")


@pytest.fixture
def projection(request: pytest.FixtureRequest):
    url = os.environ.get("FINENGINE_TEST_OPENSEARCH_URL", "")
    if not url:
        pytest.skip("нужен одноразовый OpenSearch")
    alias = f"test-indexing-{uuid.uuid4().hex}"
    name = f"{alias}-1"
    index = SearchIndex(url, alias=alias)
    index.create(name, index_body(dimension=3))
    index.switch_alias(name)
    try:
        yield index, name
    finally:
        index.delete_index(name)


def _document(doc_id: str, workspace_id: str = "F1") -> DocumentMeta:
    return DocumentMeta(
        doc_id=doc_id,
        filename=f"{doc_id}.txt",
        owner_id=7,
        workspace_id=workspace_id,
        meta_card={"issuer": "Сбербанк", "doc_kind": "отчётность", "period": "2024"},
    )


def test_index_state_survives_a_document_type_patch(
    store: SqlDocumentStore, projection, local_blobs: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    index, name = projection
    store.add_document(_document("indexed"), [])
    save_document_text("indexed", "===PAGE 1===\nРиск ликвидности раскрыт.")
    DocumentIndexer(store, index, FakeEmbeddings()).index_document("indexed", index_name=name)
    monkeypatch.setattr(api, "get_store", lambda: store)

    response = api.patch_document(
        "indexed",
        api.DocumentTypePatch(doc_type="other"),
        User(id=7, username="owner"),
    )

    assert response["doc_type"] == "other"
    assert store.get_index_state("indexed") == "indexed"
    records = index.document_records("indexed", index_name=name)
    assert {record["record_kind"] for record in records} == {"document", "chunk"}
    assert all(record["workspace_id"] == "F1" for record in records)


def test_existing_document_table_gets_index_state_column(бд: str) -> None:
    engine = движок(бд)
    with engine.begin() as connection:
        connection.execute(
            text("CREATE TABLE document (doc_id VARCHAR PRIMARY KEY, owner_id INT, meta JSONB)")
        )

    store = SqlDocumentStore(бд)
    store.set_index_state("missing", "failed:test")

    columns = {column["name"] for column in inspect(engine).get_columns("document")}
    assert "index_state" in columns
    assert store.get_document("missing") is None


def test_reindex_with_fewer_pages_removes_old_chunks(
    store: SqlDocumentStore, projection, local_blobs: None
) -> None:
    index, name = projection
    store.add_document(_document("shortened"), [])
    save_document_text("shortened", "===PAGE 1===\nПервый.\n===PAGE 2===\nВторой.")
    indexer = DocumentIndexer(store, index, FakeEmbeddings())
    indexer.index_document("shortened", index_name=name)
    save_document_text("shortened", "===PAGE 1===\nТолько новый текст.")

    indexer.index_document("shortened", index_name=name)

    records = index.document_records("shortened", index_name=name)
    chunks = [record for record in records if record["record_kind"] == "chunk"]
    assert len(chunks) == 1
    assert chunks[0]["content"] == "Только новый текст."


def test_deleted_document_is_not_recreated_by_the_state_write(
    store: SqlDocumentStore, projection, local_blobs: None
) -> None:
    index, name = projection
    store.add_document(_document("deleted-race"), [])
    save_document_text("deleted-race", "===PAGE 1===\nТекст.")
    indexer = DocumentIndexer(
        store,
        index,
        FakeEmbeddings(on_embed=lambda: store.remove_document("deleted-race")),
    )

    indexer.index_document("deleted-race", index_name=name)

    assert store.get_document("deleted-race") is None
    assert index.document_records("deleted-race", index_name=name) == []


def test_stale_embedding_cannot_overwrite_a_newer_document_text(
    store: SqlDocumentStore, projection, local_blobs: None
) -> None:
    index, name = projection
    store.add_document(_document("stale"), [])
    save_document_text("stale", "Прежний текст.")
    indexer = DocumentIndexer(store, index, FakeEmbeddings())
    indexer.index_document("stale", index_name=name)
    stale_indexer = DocumentIndexer(
        store, index, FakeEmbeddings(on_embed=lambda: save_document_text("stale", "Новый текст."))
    )

    with pytest.raises(DocumentChangedDuringIndexing):
        stale_indexer.index_document("stale", index_name=name)
    indexer.index_document("stale", index_name=name)

    records = index.document_records("stale", index_name=name)
    assert [record["content"] for record in records if record["record_kind"] == "chunk"] == [
        "Новый текст."
    ]


class AliasSwitchOnMapping(SearchIndex):
    next_index: str

    def index_meta(self, index_name: str | None = None) -> dict:
        meta = super().index_meta(index_name)
        self.switch_alias(self.next_index)
        return meta


def test_index_write_remains_on_the_index_whose_model_was_checked(
    store: SqlDocumentStore, projection, local_blobs: None
) -> None:
    index, old_name = projection
    new_name = f"{index.alias}-new"
    switching = AliasSwitchOnMapping(os.environ["FINENGINE_TEST_OPENSEARCH_URL"], index.alias)
    switching.next_index = new_name
    index.create(new_name, index_body(dimension=3, model="other-model"))
    store.add_document(_document("alias-race"), [])
    save_document_text("alias-race", "===PAGE 1===\nТекст.")

    try:
        DocumentIndexer(store, switching, FakeEmbeddings()).index_document("alias-race")

        assert index.resolve_alias() == new_name
        assert index.document_records("alias-race", index_name=new_name) == []
        assert len(index.document_records("alias-race", index_name=old_name)) == 2
    finally:
        index.delete_index(new_name)


def test_pymupdf_publishes_physical_page_numbers_for_search(
    store: SqlDocumentStore, local_blobs: None, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    source = tmp_path / "source.pdf"
    with fitz.open() as pdf:
        pdf.new_page().insert_textbox(
            (40, 40, 500, 300), "First page discusses strategy and ownership in detail." * 5
        )
        pdf.new_page().insert_textbox(
            (40, 40, 500, 300), "Second page describes operations and expected results." * 5
        )
        pdf.save(source)
    store.add_document(_document("pdf"), [])
    monkeypatch.setattr(api, "get_store", lambda: store)
    monkeypatch.setattr(settings, "opensearch_url", "")

    api._parse_doc_in_background(str(source), "pdf", "source.pdf", ocr_engine="pymupdf")

    published = document_text("pdf")
    assert published is not None
    chunks = chunk_document("pdf", published)
    assert [(chunk.page, chunk.content.split()[0]) for chunk in chunks] == [
        (1, "First"),
        (2, "Second"),
    ]


def test_provider_error_details_never_reach_logs_or_index_state(
    store: SqlDocumentStore,
    projection,
    local_blobs: None,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    store.add_document(_document("provider-error"), [])
    save_document_text("provider-error", "Текст.")
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
    monkeypatch.setattr(indexing, "get_store", lambda: store)
    monkeypatch.setattr(settings, "opensearch_url", os.environ["FINENGINE_TEST_OPENSEARCH_URL"])
    monkeypatch.setattr(indexing.EmbeddingGateway, "embed", Mock(side_effect=error))

    indexing.index_document("provider-error", index_name=projection[1])

    state = store.get_index_state("provider-error")
    assert state is not None and state.startswith("failed:")
    for private_value in ("test-body", "test-header"):
        assert private_value not in state
        assert private_value not in caplog.text


def test_search_outage_does_not_fail_the_real_background_upload(
    store: SqlDocumentStore,
    local_blobs: None,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    source = tmp_path / "source.txt"
    source.write_text("===PAGE 1===\nОпубликованный текст.", encoding="utf-8")
    meta = _document("upload-outage").model_copy(update={"file_path": str(source)})
    store.add_document(meta, [])
    monkeypatch.setattr(api, "get_store", lambda: store)
    monkeypatch.setattr(settings, "opensearch_url", "http://127.0.0.1:19201")
    monkeypatch.setattr(
        indexing.EmbeddingGateway,
        "from_settings",
        classmethod(lambda cls, config: FakeEmbeddings()),
    )

    api._parse_doc_in_background(str(source), "upload-outage", "source.txt")

    assert document_text_exists("upload-outage")
    assert store.get_index_state("upload-outage").startswith("failed:")


def test_delete_route_returns_200_when_search_is_unreachable(
    store: SqlDocumentStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    store.add_document(_document("delete-outage").model_copy(update={"file_path": ""}), [])
    monkeypatch.setattr(api, "get_store", lambda: store)
    monkeypatch.setattr(settings, "opensearch_url", "http://127.0.0.1:19201")
    api.app.dependency_overrides[api.get_current_user] = lambda: User(id=7, username="owner")
    try:
        response = TestClient(api.app).delete("/documents/delete-outage")
    finally:
        api.app.dependency_overrides.pop(api.get_current_user, None)

    assert response.status_code == 200
    assert store.get_document("delete-outage") is None
