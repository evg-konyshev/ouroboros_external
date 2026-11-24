from __future__ import annotations

from finengine.config import settings

INDEX_ALIAS = "finengine-document-search"


def index_body(
    dimension: int,
    provider: str = settings.embedding_provider,
    model: str = settings.embedding_model,
) -> dict:
    if dimension <= 0:
        raise ValueError("dimension должен быть положительным")
    return {
        "settings": {
            "number_of_shards": 1,
            "number_of_replicas": 0,
            "knn": True,
        },
        "mappings": {
            "dynamic": "strict",
            "_meta": {
                "provider": provider,
                "model": model,
                "dimension": dimension,
            },
            "properties": {
                "record_kind": {"type": "keyword"},
                "document_id": {"type": "keyword"},
                "workspace_id": {"type": "keyword"},
                "chunk_id": {"type": "keyword"},
                "title": {"type": "text", "analyzer": "russian"},
                "company": {"type": "text", "analyzer": "russian"},
                "company_key": {"type": "keyword"},
                "doc_kind": {"type": "text", "analyzer": "russian"},
                "period": {"type": "keyword"},
                "language": {"type": "keyword"},
                "description": {"type": "text", "analyzer": "russian"},
                "headings": {"type": "text", "analyzer": "russian"},
                "content": {"type": "text", "analyzer": "russian"},
                "page": {"type": "integer"},
                "section_path": {"type": "keyword"},
                "chunk_type": {"type": "keyword"},
                "vector": {
                    "type": "knn_vector",
                    "dimension": dimension,
                    "method": {
                        "name": "hnsw",
                        "engine": "lucene",
                        "space_type": "cosinesimil",
                        "parameters": {"m": 16, "ef_construction": 100},
                    },
                },
            },
        },
    }
