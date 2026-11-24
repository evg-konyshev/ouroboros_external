from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import Protocol

from finengine.assistant import ouro_view
from finengine.assistant.goal_tools import OPEN_KIND
from finengine.assistant.jobs import JobBoard
from finengine.assistant.loop_desk import LoopDesk
from finengine.assistant.loop_tools import no_loop
from finengine.assistant.read_tools import BEFORE_OPEN
from finengine.assistant.tool_call import BoundCall
from finengine.assistant.turn_record import TurnRecord
from finengine.auth.db import User
from finengine.config import settings
from finengine.ingestion.document_store import get_store
from finengine.search.client import SearchIndex, SearchUnavailable
from finengine.search.embeddings import EmbeddingGateway, EmbeddingUnavailable
from finengine.search.search import DocumentSearch, SearchResult


class SearchPort(Protocol):
    def search(
        self,
        allowed_ids: list[str],
        query: str,
        doc_ids: list[str] | None = None,
        limit: int | None = None,
        mode: str = "hybrid",
    ) -> SearchResult: ...


SearchFactory = Callable[[TurnRecord], SearchPort]


def _default_search(record: TurnRecord) -> DocumentSearch:
    user = User(
        id=record.user_id,
        username=record.owner_id,
        is_admin=record.is_admin,
    )
    return DocumentSearch(
        get_store(),
        SearchIndex(settings.opensearch_url) if settings.opensearch_url else None,
        EmbeddingGateway.from_settings(settings),
        user,
    )


class SearchTool:
    name = "aifa_search"
    root_only = False

    def __init__(
        self,
        board: JobBoard,
        desk: LoopDesk,
        search_factory: SearchFactory | None = None,
    ) -> None:
        self._board = board
        self._desk = desk
        self._search_factory = search_factory or _default_search

    async def run(self, call: BoundCall) -> str:
        loop = await self._desk.open_loop_of(call)
        if loop is None and self._board.of_kind(call.ctx.run_id, OPEN_KIND) is None:
            return no_loop(BEFORE_OPEN)
        query = call.args.get("query")
        doc_ids = call.args.get("doc_ids")
        limit = call.args.get("limit")
        mode = call.args.get("mode", "hybrid")
        if not isinstance(query, str) or not query.strip():
            return ouro_view.refusal(ouro_view.Refusal.BAD_ARGUMENTS, "query — непустая строка.")
        if doc_ids is not None and (
            not isinstance(doc_ids, list)
            or not all(isinstance(doc_id, str) and doc_id for doc_id in doc_ids)
        ):
            return ouro_view.refusal(
                ouro_view.Refusal.BAD_ARGUMENTS, "doc_ids — список непустых id."
            )
        if limit is not None and (type(limit) is not int or not 1 <= limit <= 10):
            return ouro_view.refusal(
                ouro_view.Refusal.BAD_ARGUMENTS, "limit — целое число от 1 до 10."
            )
        if not isinstance(mode, str) or mode not in {"hybrid", "lexical"}:
            return ouro_view.refusal(
                ouro_view.Refusal.BAD_ARGUMENTS, 'mode — "hybrid" или "lexical".'
            )
        if doc_ids is not None and any(doc_id not in call.record.doc_ids for doc_id in doc_ids):
            return ouro_view.refusal(
                ouro_view.Refusal.NO_DOCUMENT,
                "Такого документа в этом ходе нет: список — aifa_doc без аргументов.",
            )
        search = self._search_factory(call.record)
        try:
            result = await asyncio.to_thread(
                search.search,
                list(call.record.doc_ids),
                query.strip(),
                doc_ids,
                limit,
                mode,
            )
        except EmbeddingUnavailable:
            return ouro_view.refusal(
                ouro_view.Refusal.SEARCH_DOWN,
                'поиск недоступен; точное совпадение ищет mode="lexical"',
            )
        except SearchUnavailable as error:
            return ouro_view.refusal(ouro_view.Refusal.SEARCH_DOWN, str(error))
        return ouro_view.search_hits(
            result.hits,
            degraded=result.degraded,
            limit=settings.ouroboros_result_limit,
        )
