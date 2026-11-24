from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Callable

from botocore.exceptions import BotoCoreError, ClientError
from opensearchpy import OpenSearchException

from finengine.assistant.doc_pages import Match, find, pages_of
from finengine.auth.db import User
from finengine.auth.visibility import user_can_see
from finengine.models.document import DocumentMeta
from finengine.search.card import build_card
from finengine.search.client import SearchIndex, SearchUnavailable
from finengine.search.embeddings import EmbeddingUnavailable
from finengine.search.indexing import EmbeddingPort
from finengine.store.blobs import document_text
from finengine.store.documents import SqlDocumentStore

_RRF_K = 60
_SELECTED_DOCUMENTS = 5
_TEXT_READ_ERRORS = (BotoCoreError, ClientError, OSError)
_METRIC = re.compile(
    r"\d|%|рентабельн|выручк|прибыл|капитал|дивиденд|показател|млн|млрд|руб\.?|usd|rub",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class DocumentScore:
    document_id: str
    score: float


@dataclass(frozen=True)
class SearchHit:
    document_id: str
    chunk_id: str
    page: int
    section_path: str
    content: str
    score: float
    degraded: bool = False


@dataclass(frozen=True)
class SearchResult:
    hits: list[SearchHit]
    degraded: bool


def answer_size(query: str, limit: int | None = None) -> int:
    if limit is not None:
        return limit
    return 3 if _METRIC.search(query) else 8


def pool_size(candidate_size: int, documents: int) -> int:
    return min(500, max(candidate_size, documents * 2))


def _rrf(rankings: list[list[str]]) -> list[tuple[str, float]]:
    scores: dict[str, float] = {}
    for ranking in rankings:
        for rank, record_id in enumerate(ranking, 1):
            scores[record_id] = scores.get(record_id, 0.0) + 1.0 / (_RRF_K + rank)
    return sorted(scores.items(), key=lambda item: (-item[1], item[0]))


def _ranked_ids(hits: list[dict], key: str) -> list[str]:
    return list(dict.fromkeys(hit["_source"][key] for hit in hits))


def _fused_chunk_owners(lexical: list[dict], vector: list[dict]) -> list[str]:
    owners = {
        hit["_source"]["chunk_id"]: hit["_source"]["document_id"] for hit in (*lexical, *vector)
    }
    ranked = _rrf([_ranked_ids(lexical, "chunk_id"), _ranked_ids(vector, "chunk_id")])
    return list(dict.fromkeys(owners[chunk_id] for chunk_id, _ in ranked))


def _filters(record_kind: str, doc_ids: tuple[str, ...], workspace_id: str | None) -> list[dict]:
    filters: list[dict] = [
        {"term": {"record_kind": record_kind}},
        {"terms": {"document_id": list(doc_ids)}},
    ]
    if workspace_id is not None:
        filters.append({"term": {"workspace_id": workspace_id}})
    return filters


def _lexical_body(
    record_kind: str,
    doc_ids: tuple[str, ...],
    query: str,
    pool: int,
    workspace_id: str | None,
) -> dict:
    match = (
        {"match": {"content": query}}
        if record_kind == "chunk"
        else {
            "multi_match": {
                "query": query,
                "fields": [
                    "title^4",
                    "company^3",
                    "company_key^2",
                    "doc_kind^2",
                    "period",
                    "description",
                    "headings",
                ],
            }
        }
    )
    return {
        "size": pool,
        "query": {
            "bool": {"filter": _filters(record_kind, doc_ids, workspace_id), "must": [match]}
        },
        "_source": {"excludes": ["vector"]},
    }


def _knn_body(
    record_kind: str,
    doc_ids: tuple[str, ...],
    vector: list[float],
    pool: int,
    workspace_id: str | None,
) -> dict:
    return {
        "size": pool,
        "query": {
            "knn": {
                "vector": {
                    "vector": vector,
                    "k": pool,
                    "filter": {"bool": {"filter": _filters(record_kind, doc_ids, workspace_id)}},
                },
            }
        },
        "_source": {"excludes": ["vector"]},
    }


def _query_terms(query: str) -> list[str]:
    stop_words = {"что", "пишут", "о", "и", "в", "на", "по", "для", "как", "какие"}
    terms = [term for term in re.findall(r"[\w-]+", query.casefold()) if len(term) > 2]
    meaningful = [term for term in terms if term not in stop_words]
    selected = meaningful or terms or [query.casefold()]
    return [term[: 4 if len(term) <= 6 else 6] for term in selected]


def _text_matches(text: str, query: str, *, require_page_marks: bool) -> list[Match]:
    pages = pages_of(text, unmarked_page=0 if require_page_marks else 1)
    matches = [match for term in _query_terms(query) for match in find(term, pages)]
    unique = {(match.page, match.fragment): match for match in matches}
    return sorted(unique.values(), key=lambda match: match.page)


class DocumentSearch:
    def __init__(
        self,
        store: SqlDocumentStore,
        index: SearchIndex | None,
        embeddings: EmbeddingPort,
        user: User,
        text_reader: Callable[..., str | None] = document_text,
        *,
        text_exists: Callable[[str], bool] | None = None,
    ) -> None:
        self.store = store
        self.index = index
        self.embeddings = embeddings
        self.user = user
        self.text_reader = text_reader
        self.text_exists = text_exists

    def _index(self) -> SearchIndex:
        if self.index is None:
            raise SearchUnavailable("FINENGINE_OPENSEARCH_URL не задан")
        return self.index

    def _query_vector(self, query: str) -> list[float]:
        try:
            return self.embeddings.embed([query])[0]
        except Exception as error:  # noqa: BLE001 — внешний провайдер эмбеддингов
            raise EmbeddingUnavailable("провайдер эмбеддингов недоступен") from error

    def _permitted_document(self, doc_id: str, allowed: set[str]) -> DocumentMeta | None:
        if doc_id not in allowed:
            return None
        meta = self.store.get_document(doc_id)
        return meta if meta is not None and user_can_see(meta, self.user) else None

    def _visible(self, doc_id: str, allowed: set[str], workspace_id: str | None = None) -> bool:
        meta = self._permitted_document(doc_id, allowed)
        if meta is None or (workspace_id is not None and meta.workspace_id != workspace_id):
            return False
        try:
            if self.text_exists is not None:
                return self.text_exists(doc_id)
            return self.text_reader(doc_id, limit=1) is not None
        except _TEXT_READ_ERRORS:
            return False

    def select_documents(
        self,
        allowed_ids: list[str],
        query: str,
        mode: str = "hybrid",
        workspace_id: str | None = None,
    ) -> list[DocumentScore]:
        allowed = tuple(dict.fromkeys(allowed_ids))
        if not allowed:
            return []
        pool = pool_size(_SELECTED_DOCUMENTS, len(allowed))
        card_hits = self._index().search(
            _lexical_body("document", allowed, query, pool, workspace_id)
        )["hits"]["hits"]
        chunk_hits = self._index().search(
            _lexical_body("chunk", allowed, query, pool, workspace_id)
        )["hits"]["hits"]
        card_rankings = [_ranked_ids(card_hits, "document_id")]
        if mode == "hybrid":
            vector = self._query_vector(query)
            card_vector = self._index().search(
                _knn_body("document", allowed, vector, pool, workspace_id)
            )["hits"]["hits"]
            chunk_vector = self._index().search(
                _knn_body("chunk", allowed, vector, pool, workspace_id)
            )["hits"]["hits"]
            card_rankings.append(_ranked_ids(card_vector, "document_id"))
            chunk_ranking = _fused_chunk_owners(chunk_hits, chunk_vector)
        else:
            chunk_ranking = _ranked_ids(chunk_hits, "document_id")
        allowed_set = set(allowed)
        return [
            DocumentScore(doc_id, score)
            for doc_id, score in _rrf([*card_rankings, chunk_ranking])
            if self._visible(doc_id, allowed_set, workspace_id)
        ][:_SELECTED_DOCUMENTS]

    def retrieve_chunks(
        self,
        doc_ids: list[str],
        query: str,
        k: int,
        mode: str = "hybrid",
    ) -> list[SearchHit]:
        selected = tuple(dict.fromkeys(doc_ids))
        if not selected or k <= 0:
            return []
        pool = pool_size(k, len(selected))
        lexical = self._index().search(_lexical_body("chunk", selected, query, pool, None))["hits"][
            "hits"
        ]
        rankings = [_ranked_ids(lexical, "chunk_id")]
        sources = {hit["_source"]["chunk_id"]: hit["_source"] for hit in lexical}
        if mode == "hybrid":
            vector = self._query_vector(query)
            vector_hits = self._index().search(_knn_body("chunk", selected, vector, pool, None))[
                "hits"
            ]["hits"]
            rankings.append(_ranked_ids(vector_hits, "chunk_id"))
            sources.update({hit["_source"]["chunk_id"]: hit["_source"] for hit in vector_hits})
        allowed_set = set(selected)
        hits: list[SearchHit] = []
        for chunk_id, score in _rrf(rankings):
            source = sources[chunk_id]
            doc_id = source["document_id"]
            if not self._visible(doc_id, allowed_set):
                continue
            hits.append(
                SearchHit(
                    document_id=doc_id,
                    chunk_id=chunk_id,
                    page=int(source.get("page") or 0),
                    section_path=source.get("section_path") or "",
                    content=source["content"],
                    score=score,
                )
            )
            if len(hits) == k:
                break
        return hits

    def search(
        self,
        allowed_ids: list[str],
        query: str,
        doc_ids: list[str] | None = None,
        limit: int | None = None,
        mode: str = "hybrid",
    ) -> SearchResult:
        size = answer_size(query, limit)
        try:
            selected = (
                [score.document_id for score in self.select_documents(allowed_ids, query, mode)]
                if doc_ids is None
                else [doc_id for doc_id in dict.fromkeys(doc_ids) if doc_id in allowed_ids]
            )
            return SearchResult(self.retrieve_chunks(selected, query, size, mode), degraded=False)
        except (OpenSearchException, SearchUnavailable):
            return self._degraded(allowed_ids, query, doc_ids, size)

    def _card_matches(self, doc_id: str, allowed: set[str], terms: list[str]) -> bool:
        meta = self._permitted_document(doc_id, allowed)
        if meta is None:
            return False
        card = build_card(meta, "")
        haystack = " ".join((card.title, card.company, card.doc_kind, card.period)).casefold()
        return any(term in haystack for term in terms)

    def _degraded(
        self,
        allowed_ids: list[str],
        query: str,
        doc_ids: list[str] | None,
        limit: int,
    ) -> SearchResult:
        unavailable: Exception | None = None
        readable = False
        try:
            allowed_set = set(allowed_ids)
            selected = list(dict.fromkeys(allowed_ids if doc_ids is None else doc_ids))
            if doc_ids is None:
                terms = _query_terms(query)
                selected.sort(
                    key=lambda doc_id: self._card_matches(doc_id, allowed_set, terms),
                    reverse=True,
                )
            hits: list[SearchHit] = []
            for doc_id in selected:
                meta = self._permitted_document(doc_id, allowed_set)
                if meta is None:
                    continue
                try:
                    text = self.text_reader(doc_id)
                except _TEXT_READ_ERRORS as error:
                    unavailable = error
                    continue
                if text is None:
                    continue
                readable = True
                matches = _text_matches(
                    text, query, require_page_marks=meta.filename.casefold().endswith(".pdf")
                )
                for index, match in enumerate(matches):
                    hits.append(
                        SearchHit(
                            document_id=doc_id,
                            chunk_id=f"degraded:{doc_id}:{match.page}:{index}",
                            page=match.page,
                            section_path="",
                            content=match.fragment,
                            score=0.0,
                            degraded=True,
                        )
                    )
                    if len(hits) == limit:
                        return SearchResult(hits, degraded=True)
        except Exception as error:  # noqa: BLE001 — контракт требует единый отказ обеих потерянных проекций
            raise SearchUnavailable("поисковый индекс и тексты документов недоступны") from error
        if unavailable is not None and not readable:
            raise SearchUnavailable(
                "поисковый индекс и тексты документов недоступны"
            ) from unavailable
        return SearchResult(hits, degraded=True)
