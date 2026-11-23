"""Тесты хранилища workspace."""
from __future__ import annotations

import pytest

from finengine.store.workspaces import WorkspaceStore

pytestmark = pytest.mark.usefixtures("бд_хранилищ")


def test_upsert_default_is_idempotent(бд):
    store = WorkspaceStore(бд)
    store.upsert_workspace(owner_id=7, workspace_id="owner:7", name="Default")
    before = store.get_workspace("owner:7")
    store.upsert_workspace(owner_id=7, workspace_id="owner:7", name="Default")
    assert store.get_workspace("owner:7") == before


def test_upsert_foreign_folder_is_rejected_and_unchanged(бд):
    store = WorkspaceStore(бд)
    store.upsert_workspace(owner_id=8, workspace_id="F1", name="Чужой")
    before = store.get_workspace("F1")
    with pytest.raises(ValueError, match="другому владельцу"):
        store.upsert_workspace(owner_id=7, workspace_id="F1", name="Перехват")
    assert store.get_workspace("F1") == before


def test_upsert_foreign_default_is_rejected(бд):
    store = WorkspaceStore(бд)
    with pytest.raises(ValueError, match="owner:"):
        store.upsert_workspace(owner_id=7, workspace_id="owner:8", name="Foreign")


def test_upsert_empty_id_is_rejected(бд):
    store = WorkspaceStore(бд)
    with pytest.raises(ValueError, match="пуст"):
        store.upsert_workspace(owner_id=7, workspace_id="", name="Empty")


def test_upsert_too_long_id_is_rejected(бд):
    store = WorkspaceStore(бд)
    with pytest.raises(ValueError, match="недопустимый"):
        store.upsert_workspace(owner_id=7, workspace_id="x" * 65, name="Long")


def test_upsert_invalid_chars_rejected(бд):
    store = WorkspaceStore(бд)
    with pytest.raises(ValueError, match="недопустимый"):
        store.upsert_workspace(owner_id=7, workspace_id="folder/id", name="Bad")


def test_get_missing_returns_none(бд):
    store = WorkspaceStore(бд)
    assert store.get_workspace("no-such-folder") is None


def test_upsert_creates_folder_workspace(бд):
    store = WorkspaceStore(бд)
    store.upsert_workspace(owner_id=7, workspace_id="F1", name="Banks")
    ws = store.get_workspace("F1")
    assert ws["owner_id"] == 7
    assert ws["name"] == "Banks"
