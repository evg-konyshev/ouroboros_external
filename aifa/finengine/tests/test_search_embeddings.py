from types import SimpleNamespace

import pytest

from finengine.search import embeddings as module
from finengine.search.embeddings import EmbeddingGateway, IndexRebuildRequired, validate_index_meta


class FakeOpenRouterEndpoint:
    def __init__(self) -> None:
        self.calls: list[list[str]] = []
        self.embeddings = self

    def __enter__(self) -> "FakeOpenRouterEndpoint":
        return self

    def __exit__(self, *args: object) -> None:
        return None

    def create(self, model: str, input: list[str]) -> SimpleNamespace:
        self.calls.append(input)
        return SimpleNamespace(
            data=[
                SimpleNamespace(index=index, embedding=[float(index), 1.0, 2.0])
                for index, _ in enumerate(input)
            ],
            usage=SimpleNamespace(prompt_tokens=len(input)),
        )


class FakeGigaChatEndpoint:
    def __init__(self, **kwargs: object) -> None:
        self.calls: list[list[str]] = []

    def __enter__(self) -> "FakeGigaChatEndpoint":
        return self

    def __exit__(self, *args: object) -> None:
        return None

    def embeddings(self, texts: list[str], model: str) -> SimpleNamespace:
        self.calls.append(texts)
        return SimpleNamespace(
            data=[
                SimpleNamespace(index=index, embedding=[3.0, 4.0]) for index, _ in enumerate(texts)
            ]
        )


def test_openrouter_batching_honours_the_batch_size(monkeypatch: pytest.MonkeyPatch) -> None:
    endpoint = FakeOpenRouterEndpoint()
    monkeypatch.setattr(module, "OpenAI", lambda **kwargs: endpoint)
    gateway = EmbeddingGateway(
        provider="openrouter",
        model="text-embedding-3-small",
        batch_size=2,
        openrouter_api_key="test",
    )

    vectors = gateway.embed(["a", "b", "c", "d", "e"])

    assert endpoint.calls == [["a", "b"], ["c", "d"], ["e"]]
    assert len(vectors) == 5
    assert all(len(vector) == 3 for vector in vectors)


def test_gigachat_uses_the_native_embeddings_endpoint(monkeypatch: pytest.MonkeyPatch) -> None:
    endpoint = FakeGigaChatEndpoint()
    monkeypatch.setattr(module, "GigaChat", lambda **kwargs: endpoint)
    gateway = EmbeddingGateway(
        provider="gigachat",
        model="Embeddings",
        batch_size=3,
        gigachat_credentials="test",
    )

    assert gateway.embed(["a", "b"]) == [[3.0, 4.0], [3.0, 4.0]]
    assert endpoint.calls == [["a", "b"]]


def test_unknown_provider_names_its_setting() -> None:
    gateway = EmbeddingGateway(provider="unknown", model="m", batch_size=1)
    with pytest.raises(ValueError, match="FINENGINE_EMBEDDING_PROVIDER"):
        gateway.embed(["a"])


def test_changed_model_or_dimension_requires_a_rebuild() -> None:
    meta = {"provider": "gigachat", "model": "Embeddings", "dimension": 3}
    with pytest.raises(IndexRebuildRequired, match="нужна пересборка"):
        validate_index_meta(meta, provider="gigachat", model="Embeddings", dimension=4)
    with pytest.raises(IndexRebuildRequired, match="нужна пересборка"):
        validate_index_meta(meta, provider="gigachat", model="other", dimension=3)
