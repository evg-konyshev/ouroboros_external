from __future__ import annotations

import os
import uuid
from pathlib import Path

import pytest

from finengine.auth.db import User
from finengine.config import settings
from finengine.models.document import DocumentMeta
from finengine.search.client import SearchIndex
from finengine.search.index_schema import index_body
from finengine.search.indexing import DocumentIndexer
from finengine.search.search import DocumentSearch
from finengine.store.blobs import document_text, save_document_text
from finengine.store.documents import SqlDocumentStore

pytestmark = pytest.mark.usefixtures("бд_хранилищ")


class FakeEmbeddings:
    provider = "gigachat"
    model = "Embeddings"

    def __init__(self) -> None:
        self.calls = 0

    def embed(self, texts: list[str]) -> list[list[float]]:
        self.calls += 1
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
    alias = f"test-search-{uuid.uuid4().hex}"
    name = f"{alias}-1"
    index = SearchIndex(url, alias=alias)
    index.create(name, index_body(dimension=3))
    index.switch_alias(name)
    try:
        yield index
    finally:
        index.delete_index(name)


def _publish(
    store: SqlDocumentStore,
    index: SearchIndex,
    embeddings: FakeEmbeddings,
    doc_id: str,
    text: str,
    workspace_id: str,
    issuer: str = "",
) -> None:
    store.add_document(
        DocumentMeta(
            doc_id=doc_id,
            filename=f"{doc_id}.txt",
            owner_id=7,
            workspace_id=workspace_id,
            meta_card={"issuer": issuer, "doc_kind": "отчётность", "period": "2024"},
        ),
        [],
    )
    save_document_text(doc_id, text)
    DocumentIndexer(store, index, embeddings).index_document(doc_id)


def _search(
    store: SqlDocumentStore, index: SearchIndex, embeddings: FakeEmbeddings
) -> DocumentSearch:
    return DocumentSearch(
        store,
        index,
        embeddings,
        User(id=7, username="owner"),
    )


def test_sparse_card_is_selected_through_its_matching_chunk(
    store: SqlDocumentStore, projection: SearchIndex, local_blobs: None
) -> None:
    embeddings = FakeEmbeddings()
    _publish(
        store, projection, embeddings, "sparse", "===PAGE 1===\nРиск ликвидности раскрыт.", "F1"
    )
    _publish(store, projection, embeddings, "other", "===PAGE 1===\nДругой предмет.", "F1", "Банк")
    embeddings.calls = 0

    selected = _search(store, projection, embeddings).select_documents(
        ["sparse", "other"], "ликвидность", mode="lexical"
    )

    assert selected[0].document_id == "sparse"
    assert embeddings.calls == 0


def test_hits_outside_allowed_ids_and_orphans_are_not_returned(
    store: SqlDocumentStore, projection: SearchIndex, local_blobs: None
) -> None:
    embeddings = FakeEmbeddings()
    _publish(store, projection, embeddings, "allowed", "===PAGE 1===\nРиск ликвидности.", "F1")
    _publish(store, projection, embeddings, "foreign", "===PAGE 1===\nРиск ликвидности.", "F1")
    search = _search(store, projection, embeddings)

    outside = search.retrieve_chunks(["allowed"], "ликвидность", 10, mode="lexical")
    store.remove_document("foreign")
    orphan = search.retrieve_chunks(["foreign"], "ликвидность", 10, mode="lexical")

    assert {hit.document_id for hit in outside} == {"allowed"}
    assert orphan == []


def test_workspace_filter_is_optional(
    store: SqlDocumentStore, projection: SearchIndex, local_blobs: None
) -> None:
    embeddings = FakeEmbeddings()
    _publish(store, projection, embeddings, "first", "===PAGE 1===\nРиск ликвидности.", "F1")
    _publish(store, projection, embeddings, "second", "===PAGE 1===\nРиск ликвидности.", "F2")
    search = _search(store, projection, embeddings)

    both = search.select_documents(["first", "second"], "ликвидность", mode="lexical")
    one = search.select_documents(
        ["first", "second"], "ликвидность", mode="lexical", workspace_id="F2"
    )

    assert {score.document_id for score in both} == {"first", "second"}
    assert {score.document_id for score in one} == {"second"}


def test_pipeline_selects_first_and_lexical_never_calls_embeddings(
    store: SqlDocumentStore, projection: SearchIndex, local_blobs: None
) -> None:
    embeddings = FakeEmbeddings()
    _publish(store, projection, embeddings, "one", "===PAGE 1===\nРиск ликвидности.", "F1")
    _publish(store, projection, embeddings, "two", "===PAGE 1===\nКадровая политика.", "F1")
    search = _search(store, projection, embeddings)
    embeddings.calls = 0

    result = search.search(["one", "two"], "ликвидность", mode="lexical")

    assert result.degraded is False
    assert {hit.document_id for hit in result.hits} == {"one"}
    assert embeddings.calls == 0


class RecordingTexts:
    def __init__(self) -> None:
        self.read: list[str] = []

    def __call__(self, doc_id: str, limit: int | None = None) -> str | None:
        self.read.append(doc_id)
        return document_text(doc_id, limit=limit)


def test_unreachable_index_degrades_to_allowed_published_texts_only(
    store: SqlDocumentStore, projection: SearchIndex, local_blobs: None
) -> None:
    embeddings = FakeEmbeddings()
    _publish(store, projection, embeddings, "visible", "===PAGE 4===\nРиск ликвидности.", "F1")
    _publish(store, projection, embeddings, "hidden", "===PAGE 5===\nРиск ликвидности.", "F1")
    texts = RecordingTexts()
    search = DocumentSearch(
        store,
        SearchIndex("http://127.0.0.1:19201", alias=projection.alias),
        embeddings,
        User(id=7, username="owner"),
        text_reader=texts,
    )

    result = search.search(["visible"], "ликвидности", doc_ids=["visible"], limit=3)

    assert result.degraded is True
    assert len(result.hits) == 1
    assert result.hits[0].page == 4
    assert result.hits[0].degraded is True
    assert set(texts.read) == {"visible"}


def test_live_hybrid_paraphrase(
    store: SqlDocumentStore, projection: SearchIndex, local_blobs: None
) -> None:
    if os.environ.get("FINENGINE_TEST_LIVE_EMBEDDINGS") != "1":
        pytest.skip("живой провайдер эмбеддингов включается FINENGINE_TEST_LIVE_EMBEDDINGS=1")
    from finengine.search.embeddings import EmbeddingGateway

    embeddings = EmbeddingGateway.from_settings(settings)
    _publish(store, projection, embeddings, "live", "===PAGE 1===\nРиск ликвидности.", "F1")  # type: ignore[arg-type]
    search = DocumentSearch(store, projection, embeddings, User(id=7, username="owner"))

    result = search.search(["live"], "проблемы с ликвидностью")

    assert result.hits


def test_hybrid_executes_both_vector_legs(
    store: SqlDocumentStore, projection: SearchIndex, local_blobs: None
) -> None:
    embeddings = FakeEmbeddings()
    _publish(store, projection, embeddings, "hybrid", "===PAGE 1===\nРиск ликвидности.", "F1")
    search = _search(store, projection, embeddings)
    embeddings.calls = 0

    result = search.search(["hybrid"], "проблемы с ликвидностью")

    assert result.degraded is False
    assert result.hits
    assert result.hits[0].document_id == "hybrid"
    assert embeddings.calls == 2


def test_hybrid_ranks_the_allowed_records_before_taking_top_k(
    store: SqlDocumentStore, projection: SearchIndex, local_blobs: None
) -> None:
    embeddings = FakeEmbeddings()
    _publish(store, projection, embeddings, "allowed", "===PAGE 2===\nРиск ликвидности.", "F1")
    records = list(projection.document_records("allowed"))
    card_id = f"allowed:card:{projection.card_revisions()['allowed']}"
    for record in records:
        record["vector"] = [1.0, 0.0, 0.0]
    projection.bulk(
        [{"id": record.get("chunk_id", card_id), "source": record} for record in records]
    )
    projection.bulk(
        [
            {
                "id": f"other-{number}",
                "source": {
                    "record_kind": "chunk",
                    "document_id": "other",
                    "workspace_id": "F2",
                    "chunk_id": f"other-{number}",
                    "content": "Иной текст.",
                    "vector": [1.0, 2.0, 3.0],
                },
            }
            for number in range(30)
        ]
    )

    result = _search(store, projection, embeddings).search(["allowed"], "коэффициент покрытия")

    assert [(hit.document_id, hit.page) for hit in result.hits] == [("allowed", 2)]


def test_explicit_doc_ids_are_intersected_with_allowed_ids(
    store: SqlDocumentStore, projection: SearchIndex, local_blobs: None
) -> None:
    embeddings = FakeEmbeddings()
    _publish(store, projection, embeddings, "allowed", "===PAGE 1===\nРиск ликвидности.", "F1")
    _publish(store, projection, embeddings, "foreign", "===PAGE 1===\nРиск ликвидности.", "F1")

    result = _search(store, projection, embeddings).search(
        ["allowed"], "ликвидность", doc_ids=["foreign"], mode="lexical"
    )

    assert result.hits == []


def test_degraded_broad_query_selects_documents_by_card_terms(
    store: SqlDocumentStore, projection: SearchIndex, local_blobs: None
) -> None:
    embeddings = FakeEmbeddings()
    _publish(
        store,
        projection,
        embeddings,
        "broad",
        "===PAGE 2===\nСбербанк раскрыл риски.",
        "F1",
        "Сбербанк",
    )
    search = DocumentSearch(
        store,
        SearchIndex("http://127.0.0.1:19201", alias=projection.alias),
        embeddings,
        User(id=7, username="owner"),
    )

    result = search.search(["broad"], "что пишут о сбербанке")

    assert result.degraded is True
    assert [hit.document_id for hit in result.hits] == ["broad"]


def test_degraded_query_found_only_in_body_still_returns_a_passage(
    store: SqlDocumentStore, projection: SearchIndex, local_blobs: None
) -> None:
    embeddings = FakeEmbeddings()
    _publish(store, projection, embeddings, "body-only", "===PAGE 3===\nРиски раскрыты.", "F1")
    search = DocumentSearch(
        store,
        SearchIndex("http://127.0.0.1:19201", alias=projection.alias),
        embeddings,
        User(id=7, username="owner"),
    )

    result = search.search(["body-only"], "риски")

    assert result.degraded is True
    assert [hit.document_id for hit in result.hits] == ["body-only"]
    assert result.hits[0].page == 3


def test_degraded_card_match_does_not_hide_other_allowed_texts(
    store: SqlDocumentStore, local_blobs: None
) -> None:
    store.add_document(DocumentMeta(doc_id="card", filename="Риски.txt", owner_id=7), [])
    store.add_document(DocumentMeta(doc_id="body", filename="Отчёт.txt", owner_id=7), [])
    save_document_text("card", "===PAGE 1===\nУправление капиталом.")
    save_document_text("body", "===PAGE 4===\nРиски ликвидности.")
    search = DocumentSearch(store, None, FakeEmbeddings(), User(id=7, username="owner"))

    result = search.search(["card", "body"], "риски")

    assert [(hit.document_id, hit.page) for hit in result.hits] == [("body", 4)]


class ReadableTexts:
    def __call__(self, doc_id: str, limit: int | None = None) -> str | None:
        if doc_id == "unavailable":
            raise OSError("текст недоступен")
        return document_text(doc_id, limit=limit)


def test_indexed_search_skips_an_unreachable_sidecar_without_losing_readable_hits(
    store: SqlDocumentStore, projection: SearchIndex, local_blobs: None
) -> None:
    embeddings = FakeEmbeddings()
    _publish(store, projection, embeddings, "unavailable", "===PAGE 1===\nРиски.", "F1")
    _publish(store, projection, embeddings, "readable", "===PAGE 4===\nРиски ликвидности.", "F1")
    search = DocumentSearch(
        store, projection, embeddings, User(id=7, username="owner"), text_reader=ReadableTexts()
    )

    result = search.search(["unavailable", "readable"], "риски", mode="lexical")

    assert not result.degraded
    assert [(hit.document_id, hit.page) for hit in result.hits] == [("readable", 4)]


def test_degraded_skips_unreachable_text_and_does_not_invent_legacy_pdf_pages(
    store: SqlDocumentStore, local_blobs: None
) -> None:
    store.add_document(
        DocumentMeta(doc_id="unavailable", filename="unavailable.txt", owner_id=7), []
    )
    store.add_document(DocumentMeta(doc_id="legacy", filename="legacy.pdf", owner_id=7), [])
    save_document_text("legacy", "Риски ликвидности.")
    search = DocumentSearch(
        store, None, FakeEmbeddings(), User(id=7, username="owner"), text_reader=ReadableTexts()
    )

    result = search.search(["unavailable", "legacy"], "риски")

    assert [(hit.document_id, hit.page) for hit in result.hits] == [("legacy", 0)]
