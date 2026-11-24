from __future__ import annotations

from opensearchpy import OpenSearch, helpers

from finengine.search.index_schema import INDEX_ALIAS


class SearchUnavailable(RuntimeError):
    pass


class SearchIndex:
    def __init__(
        self,
        url: str,
        alias: str = INDEX_ALIAS,
        client: OpenSearch | None = None,
    ) -> None:
        if not url and client is None:
            raise SearchUnavailable("FINENGINE_OPENSEARCH_URL не задан")
        self._client = client or OpenSearch(
            hosts=[url],
            timeout=10,
            max_retries=0,
            retry_on_timeout=False,
        )
        self.alias = alias

    def create(self, name: str, body: dict) -> None:
        self._client.indices.create(index=name, body=body)

    def switch_alias(self, name: str) -> None:
        previous = self.resolve_alias()
        actions = []
        if previous and previous != name:
            actions.append({"remove": {"index": previous, "alias": self.alias}})
        actions.append({"add": {"index": name, "alias": self.alias}})
        self._client.indices.update_aliases(body={"actions": actions})

    def resolve_alias(self) -> str | None:
        if not self._client.indices.exists_alias(name=self.alias):
            return None
        aliases = self._client.indices.get_alias(name=self.alias)
        return sorted(aliases)[0] if aliases else None

    def delete_index(self, name: str) -> None:
        if self._client.indices.exists(index=name):
            self._client.indices.delete(index=name)

    def target(self, index_name: str | None = None) -> str:
        return index_name or self.resolve_alias() or self.alias

    def index_meta(self, index_name: str | None = None) -> dict:
        mapping = self._client.indices.get_mapping(index=self.target(index_name))
        return next(iter(mapping.values()))["mappings"].get("_meta", {})

    def bulk(self, records: list[dict], index_name: str | None = None) -> int:
        if not records:
            return 0
        target = self.target(index_name)
        actions = [
            {
                "_index": target,
                "_id": record["id"],
                "_source": record["source"],
            }
            for record in records
        ]
        succeeded, _ = helpers.bulk(self._client, actions, refresh=True, raise_on_error=True)
        return int(succeeded)

    def delete_document(self, document_id: str, index_name: str | None = None) -> None:
        result = self._client.delete_by_query(
            index=self.target(index_name),
            body={"query": {"term": {"document_id": document_id}}},
            refresh=True,
            conflicts="proceed",
        )
        if result.get("timed_out") or result.get("failures") or result.get("version_conflicts"):
            raise SearchUnavailable("поисковые записи документа удалены не полностью")

    def document_records(self, document_id: str, index_name: str | None = None) -> list[dict]:
        response = self._client.search(
            index=self.target(index_name),
            body={
                "query": {"term": {"document_id": document_id}},
                "size": 10_000,
                "sort": [{"chunk_id": {"order": "asc"}}],
                "_source": {"excludes": ["vector"]},
            },
        )
        return [hit["_source"] for hit in response["hits"]["hits"]]

    def card_ids(self, index_name: str | None = None) -> set[str]:
        return set(self.card_revisions(index_name))

    def card_revisions(self, index_name: str | None = None) -> dict[str, str]:
        hits = helpers.scan(
            self._client,
            index=self.target(index_name),
            query={
                "query": {"term": {"record_kind": "document"}},
                "_source": ["document_id"],
                "size": 1000,
            },
        )
        return {hit["_source"]["document_id"]: hit["_id"].rpartition(":card:")[2] for hit in hits}

    def search(self, body: dict, index_name: str | None = None) -> dict:
        return self._client.search(index=self.target(index_name), body=body)
