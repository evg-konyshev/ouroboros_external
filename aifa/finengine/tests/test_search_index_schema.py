import pytest

from finengine.search.index_schema import index_body


def test_index_mapping_is_strict_and_namespaces_records() -> None:
    body = index_body(dimension=3, provider="gigachat", model="Embeddings")

    assert body["mappings"]["dynamic"] == "strict"
    assert body["mappings"]["_meta"] == {
        "provider": "gigachat",
        "model": "Embeddings",
        "dimension": 3,
    }
    properties = body["mappings"]["properties"]
    assert properties["document_id"] == {"type": "keyword"}
    assert properties["workspace_id"] == {"type": "keyword"}
    assert properties["record_kind"] == {"type": "keyword"}
    assert properties["vector"]["type"] == "knn_vector"
    assert properties["vector"]["dimension"] == 3
    assert properties["vector"]["method"]["name"] == "hnsw"
    assert properties["vector"]["method"]["space_type"] == "cosinesimil"


def test_index_mapping_refuses_a_zero_dimension() -> None:
    with pytest.raises(ValueError, match="dimension"):
        index_body(dimension=0)
