import os
import uuid

import pytest

from finengine.search.client import SearchIndex
from finengine.search.index_schema import index_body

_URL = os.environ.get("FINENGINE_TEST_OPENSEARCH_URL", "")
pytestmark = pytest.mark.skipif(not _URL, reason="нужен одноразовый OpenSearch")


def test_create_and_switch_alias() -> None:
    suffix = uuid.uuid4().hex
    alias = f"test-search-alias-{suffix}"
    first = f"test-search-{suffix}-1"
    second = f"test-search-{suffix}-2"
    index = SearchIndex(_URL, alias=alias)
    try:
        index.create(first, index_body(dimension=3))
        index.switch_alias(first)
        assert index.resolve_alias() == first

        index.create(second, index_body(dimension=3))
        index.switch_alias(second)
        assert index.resolve_alias() == second
    finally:
        index.delete_index(first)
        index.delete_index(second)
