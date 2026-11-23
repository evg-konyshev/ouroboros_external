"""Workspace принадлежность документов."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from threading import Barrier
from typing import Callable

import pytest
from sqlalchemy import text

from finengine.models.document import DocumentMeta
from finengine.store.documents import SqlDocumentStore
from finengine.store.engine import движок

pytestmark = pytest.mark.usefixtures("бд_хранилищ")


@pytest.fixture
def база(бд) -> SqlDocumentStore:
    return SqlDocumentStore(бд)


def _док(doc_id: str, owner: int = 7, workspace_id: str = "") -> DocumentMeta:
    return DocumentMeta(
        doc_id=doc_id, filename="x.pdf", owner_id=owner, workspace_id=workspace_id,
    )


@dataclass(frozen=True)
class _DocumentReadBarrier:
    read: Callable[[str], tuple[int, str | None] | None]
    barrier: Barrier

    def __call__(self, doc_id: str) -> tuple[int, str | None] | None:
        result = self.read(doc_id)
        self.barrier.wait(timeout=5)
        return result


def test_upload_without_workspace_goes_to_default(база):
    база.add_document(_док("d1", owner=7), [])
    m = база.get_document("d1")
    assert m.workspace_id == "owner:7"


def test_upload_into_folder_workspace(база):
    база.add_document(_док("d2", owner=7, workspace_id="F1"), [])
    m = база.get_document("d2")
    assert m.workspace_id == "F1"


def test_re_upsert_without_workspace_keeps_existing(база):
    база.add_document(_док("d3", owner=7, workspace_id="F1"), [])
    база.add_document(_док("d3", owner=7, workspace_id=""), [])
    m = база.get_document("d3")
    assert m.workspace_id == "F1"


def test_re_upsert_cannot_change_document_owner(база):
    база.add_document(_док("d3", owner=7, workspace_id="F1"), [])
    with pytest.raises(ValueError, match="владелец документа"):
        база.add_document(_док("d3", owner=8, workspace_id=""), [])
    m = база.get_document("d3")
    assert m.owner_id == 7
    assert m.workspace_id == "F1"


def test_concurrent_insert_cannot_change_document_owner(база, monkeypatch):
    barriered = _DocumentReadBarrier(база._existing_document, Barrier(2))
    monkeypatch.setattr(база, "_existing_document", barriered)
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [
            pool.submit(база.add_document, _док("race", owner=owner), [])
            for owner in (7, 8)
        ]
        outcomes = []
        for future in futures:
            try:
                future.result()
                outcomes.append("ok")
            except ValueError:
                outcomes.append("rejected")
    assert sorted(outcomes) == ["ok", "rejected"]
    m = база.get_document("race")
    assert m.owner_id in {7, 8}
    assert m.workspace_id == f"owner:{m.owner_id}"


def test_update_meta_cannot_change_document_owner(база):
    база.add_document(_док("d3", owner=7, workspace_id="F1"), [])
    with pytest.raises(ValueError, match="владелец документа"):
        база.update_document_meta(_док("d3", owner=8, workspace_id=""))
    m = база.get_document("d3")
    assert m.owner_id == 7
    assert m.workspace_id == "F1"


def test_update_meta_does_not_reset_workspace(база):
    база.add_document(_док("d4", owner=7, workspace_id="F1"), [])
    база.update_document_meta(_док("d4", owner=7, workspace_id="").model_copy(
        update={"total_pages": 5}))
    m = база.get_document("d4")
    assert m.workspace_id == "F1"
    assert m.total_pages == 5


def test_list_documents_filters_by_workspace(база):
    база.add_document(_док("d5", owner=7, workspace_id="F1"), [])
    база.add_document(_док("d6", owner=7, workspace_id="F2"), [])
    база.add_document(_док("d7", owner=7, workspace_id=""), [])
    assert {d.doc_id for d in база.list_documents(workspace_id="F1")} == {"d5"}
    assert {d.doc_id for d in база.list_documents(workspace_id="F2")} == {"d6"}
    assert {d.doc_id for d in база.list_documents(workspace_id="owner:7")} == {"d7"}


def test_existing_table_without_column_gets_column_on_store_start(бд):
    """ADD COLUMN IF NOT EXISTS добавляет колонку, повторный старт — no-op."""
    # Под чистой схемой создаём таблицу document без workspace_id.
    eng = движок(бд)
    with eng.begin() as c:
        c.execute(text("CREATE TABLE document (doc_id VARCHAR PRIMARY KEY, owner_id INT, meta JSONB)"))
    store = SqlDocumentStore(бд)
    store.add_document(DocumentMeta(doc_id="legacy", filename="x.pdf", owner_id=7), [])
    assert store.get_document("legacy").workspace_id == "owner:7"
    # Второй старт не падает.
    SqlDocumentStore(бд)


def test_get_document_overlays_workspace_from_column(база):
    """Значение в meta JSON игнорируется, колонка — источник истины."""
    m = _док("d8", owner=7, workspace_id="F1")
    база.add_document(m, [])
    # Перезаписали meta без workspace_id
    база.update_document_meta(_док("d8", owner=7, workspace_id=""))
    fetched = база.get_document("d8")
    assert fetched.workspace_id == "F1"
