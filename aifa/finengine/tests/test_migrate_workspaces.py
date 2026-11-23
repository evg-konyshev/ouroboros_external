"""One-shot backfill workspace_id."""
from __future__ import annotations

from functools import partial

import pytest
from sqlalchemy import update

from finengine.config import settings
from finengine.models.document import DocumentMeta
from finengine.store import dialogs as dialog_store
from finengine.store.dialogs import SqlDialogStore
from finengine.store.documents import DOCUMENT, SqlDocumentStore
from finengine.store.migrate_workspaces import migrate

pytestmark = pytest.mark.usefixtures("бд_хранилищ")


@pytest.fixture
def доки(бд) -> SqlDocumentStore:
    return SqlDocumentStore(бд)


@pytest.fixture
def диалоги(бд, monkeypatch) -> SqlDialogStore:
    monkeypatch.setattr(settings, "dialogs_database_url", бд)
    dialog_store._cached = None
    return SqlDialogStore(бд)


def _док(doc_id: str, owner: int = 7) -> DocumentMeta:
    return DocumentMeta(doc_id=doc_id, filename="x.pdf", owner_id=owner)


def _save_state(диалоги: SqlDialogStore, user_id: int, state: dict) -> None:
    state.setdefault("last_project_id", None)
    state.setdefault("saved_at", None)
    диалоги.save(user_id, state)


def _nullify_workspaces(доки: SqlDocumentStore) -> None:
    with доки._engine.begin() as conn:
        conn.execute(update(DOCUMENT).values(workspace_id=None))


def _assign_f2_before_upsert(доки: SqlDocumentStore, original, **kwargs) -> None:
    with доки._engine.begin() as conn:
        conn.execute(
            update(DOCUMENT).where(DOCUMENT.c.doc_id == "d1").values(workspace_id="F2")
        )
    original(**kwargs)


def _legacy_doc(доки: SqlDocumentStore, doc_id: str, owner: int = 7) -> None:
    with доки._engine.begin() as conn:
        conn.execute(DOCUMENT.insert().values(
            doc_id=doc_id,
            owner_id=owner,
            workspace_id=None,
            meta={"doc_id": doc_id, "filename": "x.pdf", "owner_id": owner},
        ))


def test_dry_run_does_not_write_workspace_or_document(доки, диалоги):
    _legacy_doc(доки, "legacy-dry-run")
    _save_state(диалоги, 7, {"folders": [
        {"id": "F1", "doc_ids": ["legacy-dry-run"], "created_at": 100},
    ]})
    migrate(доки._engine, apply=False)
    assert not доки.get_document("legacy-dry-run").workspace_id
    assert доки.workspace_store.get_workspace("F1") is None


def test_backfill_creates_missing_default_workspace(доки, диалоги):
    _legacy_doc(доки, "legacy-default")
    _save_state(диалоги, 7, {"folders": []})
    migrate(доки._engine, apply=True)
    assert доки.get_document("legacy-default").workspace_id == "owner:7"
    assert доки.workspace_store.get_workspace("owner:7")["owner_id"] == 7


def test_document_in_one_folder_gets_it(доки, диалоги):
    доки.add_document(_док("d1"), [])
    _nullify_workspaces(доки)
    _save_state(диалоги, 7, {"folders": [
        {"id": "F1", "name": "Банки", "doc_ids": ["d1"], "created_at": 100},
    ]})
    migrate(доки._engine, apply=True)
    assert доки.get_document("d1").workspace_id == "F1"
    assert доки.workspace_store.get_workspace("F1")["name"] == "Банки"


def test_backfill_does_not_replace_workspace_assigned_concurrently(доки, диалоги, monkeypatch):
    доки.workspace_store.upsert_workspace(owner_id=7, workspace_id="F2", name="Новое")
    доки.add_document(_док("d1"), [])
    _nullify_workspaces(доки)
    _save_state(диалоги, 7, {"folders": [
        {"id": "F1", "name": "Старое", "doc_ids": ["d1"], "created_at": 100},
    ]})
    original = доки.workspace_store.upsert_workspace
    monkeypatch.setattr(
        доки.workspace_store,
        "upsert_workspace",
        partial(_assign_f2_before_upsert, доки, original),
    )
    monkeypatch.setattr(
        "finengine.store.migrate_workspaces.WorkspaceStore",
        lambda _: доки.workspace_store,
    )
    migrate(доки._engine, apply=True)
    assert доки.get_document("d1").workspace_id == "F2"


def test_backfill_foreign_folder_falls_back_to_default(доки, диалоги):
    доки.workspace_store.upsert_workspace(owner_id=8, workspace_id="F1", name="Чужой")
    before = доки.workspace_store.get_workspace("F1")
    _legacy_doc(доки, "legacy-collision", owner=7)
    _save_state(диалоги, 7, {"folders": [
        {"id": "F1", "name": "Попытка", "doc_ids": ["legacy-collision"], "created_at": 100},
    ]})
    migrate(доки._engine, apply=True)
    assert доки.get_document("legacy-collision").workspace_id == "owner:7"
    assert доки.workspace_store.get_workspace("owner:7")["owner_id"] == 7
    assert доки.workspace_store.get_workspace("F1") == before


def test_document_in_two_folders_gets_oldest(доки, диалоги):
    доки.add_document(_док("d1"), [])
    _nullify_workspaces(доки)
    _save_state(диалоги, 7, {"folders": [
        {"id": "F2", "doc_ids": ["d1"], "created_at": 200},
        {"id": "F1", "doc_ids": ["d1"], "created_at": 100},
    ]})
    migrate(доки._engine, apply=True)
    assert доки.get_document("d1").workspace_id == "F1"


def test_document_via_project_doc_ids_gets_folder(доки, диалоги):
    доки.add_document(_док("d1"), [])
    _nullify_workspaces(доки)
    _save_state(диалоги, 7, {"folders": [
        {"id": "F1", "doc_ids": [], "created_at": 100},
    ], "projects": [
        {"id": "p1", "folder_id": "F1", "doc_ids": ["d1"], "updated_at": 150},
    ]})
    migrate(доки._engine, apply=True)
    assert доки.get_document("d1").workspace_id == "F1"


def test_document_in_no_folder_gets_default(доки, диалоги):
    доки.add_document(_док("d1"), [])
    _nullify_workspaces(доки)
    _save_state(диалоги, 7, {"folders": []})
    migrate(доки._engine, apply=True)
    assert доки.get_document("d1").workspace_id == "owner:7"


def test_foreign_document_in_folder_list_gets_its_owner_default(доки, диалоги):
    доки.add_document(_док("d1", owner=8), [])
    _nullify_workspaces(доки)
    _save_state(диалоги, 7, {"folders": [{"id": "F1", "doc_ids": ["d1"], "created_at": 100}]})
    migrate(доки._engine, apply=True)
    assert доки.get_document("d1").workspace_id == "owner:8"


def test_owner_zero_gets_default(доки, диалоги):
    доки.add_document(_док("d0", owner=0), [])
    _nullify_workspaces(доки)
    _save_state(диалоги, 0, {"folders": []})
    migrate(доки._engine, apply=True)
    assert доки.get_document("d0").workspace_id == "owner:0"


def test_second_apply_changes_nothing(доки, диалоги):
    доки.add_document(_док("d1"), [])
    _nullify_workspaces(доки)
    _save_state(диалоги, 7, {"folders": [{"id": "F1", "doc_ids": ["d1"], "created_at": 100}]})
    migrate(доки._engine, apply=True)
    counts = migrate(доки._engine, apply=True)
    assert all(c == 0 for c in counts.values())
