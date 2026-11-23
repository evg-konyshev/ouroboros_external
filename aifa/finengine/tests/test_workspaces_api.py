"""Workspace_id на ручках upload и dialogs (ADR-0101)."""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

import finengine.api.main as m
import finengine.ingestion.document_store as D
from finengine.api.main import get_current_user
from finengine.auth.db import User

pytestmark = pytest.mark.usefixtures("бд_хранилищ")


MIN_PDF = b"%PDF-1.4\n" + b"0" * 2000


@pytest.fixture()
def client(tmp_path, monkeypatch, catalog):
    """Приложение с подставным каталогом и авторизацией пользователя 7."""
    monkeypatch.setattr(m.settings, "docs_dir", tmp_path)
    m.app.dependency_overrides[get_current_user] = lambda: User(
        id=7, username="t", is_admin=True)
    try:
        yield TestClient(m.app)
    finally:
        m.app.dependency_overrides.pop(get_current_user, None)


def _upload(client, data=None):
    return client.post(
        "/documents/upload",
        files={"file": ("ok.pdf", MIN_PDF, "application/pdf")},
        data={"defer_parse": "1", **(data or {})},
    )


def test_upload_without_workspace_uses_default(client):
    got = _upload(client)
    assert got.status_code == 200, got.text[:300]

    store = D.get_store()
    meta = store.get_document(got.json()["doc_id"])
    assert meta.workspace_id == "owner:7"


def test_upload_into_own_default_workspace(client):
    got = _upload(client, {"workspace_id": "owner:7"})
    assert got.status_code == 200, got.text[:300]
    store = D.get_store()
    assert store.get_document(got.json()["doc_id"]).workspace_id == "owner:7"
    assert store.workspace_store.get_workspace("owner:7")["owner_id"] == 7


def test_upload_into_existing_workspace(client):
    store = D.get_store()
    store.workspace_store.upsert_workspace(owner_id=7, workspace_id="F1", name="Банки")

    got = _upload(client, {"workspace_id": "F1"})
    assert got.status_code == 200, got.text[:300]
    assert store.get_document(got.json()["doc_id"]).workspace_id == "F1"
    assert store.workspace_store.get_workspace("F1")["name"] == "Банки"


def test_upload_into_unknown_workspace_creates_it(client):
    got = _upload(client, {"workspace_id": "new-ws-123"})
    assert got.status_code == 200, got.text[:300]

    store = D.get_store()
    assert store.get_document(got.json()["doc_id"]).workspace_id == "new-ws-123"
    ws = store.workspace_store.get_workspace("new-ws-123")
    assert ws is not None
    assert ws["owner_id"] == 7


def test_upload_into_foreign_workspace_is_404(client):
    store = D.get_store()
    store.workspace_store.upsert_workspace(owner_id=8, workspace_id="F8", name="")

    got = _upload(client, {"workspace_id": "F8"})
    assert got.status_code == 404, got.text[:300]
    assert "workspace not found" in got.json()["detail"]


def test_upload_into_owner_prefix_workspace_is_404(client, catalog):
    got = _upload(client, {"workspace_id": "owner:8"})
    assert got.status_code == 404, got.text[:300]
    assert not catalog.files, "отклонённая загрузка не должна звать каталог"


def test_upload_into_too_long_workspace_id_is_404(client, catalog):
    got = _upload(client, {"workspace_id": "x" * 65})
    assert got.status_code == 404, got.text[:300]
    assert not catalog.files, "отклонённая загрузка не должна звать каталог"
