"""C3.12: recognised page counts survive metadata writes and schema upgrades."""

from sqlalchemy import text

from finengine.models.document import DocumentMeta
from finengine.store.documents import SqlDocumentStore
from finengine.store.engine import движок


def test_text_pages_column_is_added_and_is_independent_of_metadata(бд):
    store = SqlDocumentStore(бд)
    doc = DocumentMeta(doc_id="legacy", filename="legacy.pdf", owner_id=7)
    store.add_document(doc, [])

    engine = движок(бд)
    with engine.begin() as conn:
        conn.execute(text("ALTER TABLE document DROP COLUMN text_pages"))
    engine.dispose()
    store = SqlDocumentStore(бд)
    assert store.get_document(doc.doc_id).text_pages is None

    store.set_text_pages(doc.doc_id, 5, if_missing=True)
    store.set_text_pages(doc.doc_id, 7)
    store.set_text_pages(doc.doc_id, 5, if_missing=True)
    doc.company_name = "Компания"
    store.add_document(doc, [])
    store.update_document_meta(doc)
    saved = store.get_document(doc.doc_id)
    assert saved is not None and saved.text_pages == 7
    assert saved.company_name == "Компания"
