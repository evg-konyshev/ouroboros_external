"""PUT /dialogs создаёт/обновляет workspace для валидных папок (ADR-0101, W2)."""
from __future__ import annotations

import logging

import pytest
from fastapi.testclient import TestClient

import finengine.api.main as m
import finengine.ingestion.document_store as D
import finengine.store.dialogs as dialog_store
from finengine.api.main import get_current_user
from finengine.auth.db import User

pytestmark = pytest.mark.usefixtures("бд_хранилищ")


@pytest.fixture
def client(tmp_path, monkeypatch, бд_хранилищ):
    """Приложение: диалоги, документы и workspaces в тестовом Postgres."""
    monkeypatch.setattr(m.settings, "dialogs_database_url", бд_хранилищ)
    monkeypatch.setattr(m.settings, "data_dir", tmp_path)
    dialog_store._cached = None

    m.app.dependency_overrides[get_current_user] = lambda: User(
        id=7, username="t", is_admin=True)
    D.reset_store()
    try:
        yield TestClient(m.app)
    finally:
        m.app.dependency_overrides.pop(get_current_user, None)
        D.reset_store()
        dialog_store._cached = None


def _folder(fid: str, *, name: str = "папка", updated_at: int = 1000) -> dict:
    return {
        "id": fid,
        "name": name,
        "doc_ids": [],
        "created_at": 1,
        "updated_at": updated_at,
    }


def _push(client, folders):
    return client.put("/dialogs", json={
        "projects": [],
        "folders": folders,
        "last_project_id": None,
    })


def _fail_workspace_upsert(*args, **kwargs):
    raise RuntimeError("boom")


def test_new_folder_creates_workspace(client):
    got = _push(client, [_folder("F1", name="Отчётность")])
    assert got.status_code == 200, got.text[:300]

    ws = D.get_store().workspace_store.get_workspace("F1")
    assert ws is not None
    assert ws["owner_id"] == 7
    assert ws["name"] == "Отчётность"


def test_delayed_push_names_workspace_created_by_upload(client):
    store = D.get_store()
    store.workspace_store.upsert_workspace(owner_id=7, workspace_id="F1", name="")
    got = _push(client, [_folder("F1", name="Отчётность", updated_at=1000)])
    assert got.status_code == 200, got.text[:300]
    assert store.workspace_store.get_workspace("F1")["name"] == "Отчётность"


def test_newer_rename_changes_name_and_older_does_not(client):
    _push(client, [_folder("F1", name="Старое", updated_at=1000)])
    _push(client, [_folder("F1", name="Новое", updated_at=2000)])
    assert D.get_store().workspace_store.get_workspace("F1")["name"] == "Новое"

    # Older timestamp must not roll the name back.
    _push(client, [_folder("F1", name="Старое", updated_at=500)])
    assert D.get_store().workspace_store.get_workspace("F1")["name"] == "Новое"


def test_foreign_folder_is_untouched_and_logged(client, caplog):
    store = D.get_store()
    store.workspace_store.upsert_workspace(owner_id=8, workspace_id="F1", name="Чужой")

    with caplog.at_level(logging.WARNING, logger="finengine.api.main"):
        got = _push(client, [_folder("F1", name="Попытка", updated_at=2000)])
    assert got.status_code == 200, got.text[:300]

    ws = store.workspace_store.get_workspace("F1")
    assert ws["owner_id"] == 8
    assert ws["name"] == "Чужой"
    assert "collides with another owner" in caplog.text


def test_owner_prefixed_folder_is_ignored_and_push_succeeds(client):
    got = _push(client, [_folder("owner:8", name="Запрещённая")])
    assert got.status_code == 200, got.text[:300]
    assert D.get_store().workspace_store.get_workspace("owner:8") is None


def test_own_default_prefixed_folder_is_ignored_and_push_succeeds(client):
    got = _push(client, [_folder("owner:7", name="Не папка")])
    assert got.status_code == 200, got.text[:300]
    assert D.get_store().workspace_store.get_workspace("owner:7") is None


def test_raising_upsert_does_not_fail_push(client, monkeypatch):
    monkeypatch.setattr(
        D.get_store().workspace_store, "upsert_workspace", _fail_workspace_upsert)
    got = _push(client, [_folder("F1")])
    assert got.status_code == 200, got.text[:300]


def test_folder_missing_from_push_keeps_workspace(client):
    _push(client, [_folder("F1", name="Сохраняем")])
    _push(client, [])
    ws = D.get_store().workspace_store.get_workspace("F1")
    assert ws is not None
    assert ws["name"] == "Сохраняем"
