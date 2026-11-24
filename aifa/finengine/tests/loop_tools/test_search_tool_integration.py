from __future__ import annotations

import os
import uuid
from dataclasses import replace
from pathlib import Path

import pytest
from loop_support import open_cycle

from finengine.assistant.search_tool import _default_search
from finengine.assistant.turn_record import remember
from finengine.config import settings
from finengine.models.document import DocumentMeta
from finengine.search.client import SearchIndex
from finengine.search.embeddings import EmbeddingGateway
from finengine.search.index_schema import INDEX_ALIAS, index_body
from finengine.search.indexing import DocumentIndexer
from finengine.store.blobs import save_document_text
from finengine.store.documents import SqlDocumentStore

pytestmark = pytest.mark.usefixtures("бд_хранилищ")


class FakeEmbeddings:
    provider = "gigachat"
    model = "Embeddings"

    def embed(self, texts: list[str]) -> list[list[float]]:
        return [[1.0, 2.0, 3.0] for _ in texts]


async def test_tool_api_returns_a_real_indexed_chunk(
    base,
    бд: str,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    url = os.environ.get("FINENGINE_TEST_OPENSEARCH_URL", "")
    if not url:
        pytest.skip("нужен одноразовый OpenSearch")
    monkeypatch.setattr(settings, "data_dir", tmp_path)
    monkeypatch.setattr(settings, "docs_dir", tmp_path / "docs")
    monkeypatch.setattr(settings, "s3_endpoint", "")
    monkeypatch.setattr(settings, "opensearch_url", url)
    monkeypatch.setattr(
        EmbeddingGateway,
        "from_settings",
        classmethod(lambda cls, config: FakeEmbeddings()),
    )
    alias = INDEX_ALIAS
    name = f"{alias}-{uuid.uuid4().hex}"
    index = SearchIndex(url, alias=alias)
    index.create(name, index_body(dimension=3))
    index.switch_alias(name)
    store = SqlDocumentStore(бд)
    store.add_document(
        DocumentMeta(
            doc_id="real-doc",
            filename="real-doc.txt",
            owner_id=7,
            workspace_id="F1",
            meta_card={"issuer": "Сбербанк", "doc_kind": "отчётность", "period": "2024"},
        ),
        [],
    )
    save_document_text("real-doc", "===PAGE 6===\nРиск ликвидности раскрыт.")
    DocumentIndexer(store, index, FakeEmbeddings()).index_document("real-doc")
    cycle = await open_cycle(base, doc_ids=("real-doc",))
    cycle.run.record = replace(cycle.run.record, user_id=7)
    remember(cycle.run.record)
    try:
        service = _default_search(cycle.run.record)
        meta = service.store.get_document("real-doc")
        assert meta is not None
        assert (meta.owner_id, service.user.id, service.user.is_admin) == (7, 7, False)
        assert service.text_reader("real-doc", limit=1) is not None
        assert service._visible("real-doc", {"real-doc"})
        direct = service.search(["real-doc"], "ликвидность", mode="lexical")
        assert direct.hits
        reply = await cycle.call("aifa_search", {"query": "ликвидность", "mode": "lexical"})

        assert reply.startswith("ГОТОВО")
        assert "real-doc" in reply
        assert "стр. 6" in reply
        assert "Риск ликвидности раскрыт." in reply
    finally:
        await cycle.close()
        index.delete_index(name)
