from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from gigachat import GigaChat
from openai import OpenAI

from finengine.config import EMBEDDING_PROVIDERS, Settings
from finengine.llm import traces


class IndexRebuildRequired(ValueError):
    pass


class EmbeddingError(RuntimeError):
    pass


class EmbeddingUnavailable(EmbeddingError):
    pass


def validate_index_meta(
    meta: dict,
    *,
    provider: str,
    model: str,
    dimension: int,
) -> None:
    expected = {"provider": provider, "model": model, "dimension": dimension}
    if any(meta.get(key) != value for key, value in expected.items()):
        raise IndexRebuildRequired(
            "нужна пересборка: провайдер, модель или размерность не совпадают с _meta индекса"
        )


class EmbeddingGateway:
    def __init__(
        self,
        *,
        provider: str,
        model: str,
        batch_size: int,
        openrouter_api_key: str = "",
        openrouter_base_url: str = "https://openrouter.ai/api/v1",
        gigachat_credentials: str = "",
        gigachat_scope: str = "GIGACHAT_API_CORP",
    ) -> None:
        self.provider = provider
        self.model = model
        self.batch_size = batch_size
        self.openrouter_api_key = openrouter_api_key
        self.openrouter_base_url = openrouter_base_url
        self.gigachat_credentials = gigachat_credentials
        self.gigachat_scope = gigachat_scope

    @classmethod
    def from_settings(cls, settings: Settings) -> "EmbeddingGateway":
        return cls(
            provider=settings.embedding_provider,
            model=settings.embedding_model,
            batch_size=settings.embedding_batch_size,
            openrouter_api_key=settings.openrouter_api_key,
            openrouter_base_url=settings.openrouter_base_url,
            gigachat_credentials=settings.gigachat_credentials,
            gigachat_scope=settings.gigachat_scope,
        )

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        if self.provider not in EMBEDDING_PROVIDERS:
            raise ValueError(f"FINENGINE_EMBEDDING_PROVIDER: неизвестный {self.provider!r}")
        if not texts:
            return []
        vectors = [
            vector
            for start in range(0, len(texts), self.batch_size)
            for vector in self._embed_batch(list(texts[start : start + self.batch_size]))
        ]
        dimensions = {len(vector) for vector in vectors}
        if len(dimensions) != 1:
            raise EmbeddingError("провайдер вернул векторы разной размерности")
        return vectors

    def _embed_batch(self, texts: list[str]) -> list[list[float]]:
        response = (
            self._openrouter_batch(texts)
            if self.provider == "openrouter"
            else self._gigachat_batch(texts)
        )
        data = sorted(response.data, key=lambda item: item.index)
        if len(data) != len(texts):
            raise EmbeddingError("провайдер вернул не все векторы пакета")
        self._trace(response)
        return [list(map(float, item.embedding)) for item in data]

    def _openrouter_batch(self, texts: list[str]) -> Any:
        with OpenAI(
            base_url=self.openrouter_base_url,
            api_key=self.openrouter_api_key,
            timeout=120.0,
        ) as client:
            return client.embeddings.create(model=self.model, input=texts)

    def _gigachat_batch(self, texts: list[str]) -> Any:
        with GigaChat(
            credentials=self.gigachat_credentials,
            scope=self.gigachat_scope,
            verify_ssl_certs=False,
            timeout=120.0,
        ) as client:
            return client.embeddings(texts=texts, model=self.model)

    def _trace(self, response: Any) -> None:
        usage = getattr(response, "usage", None)
        tokens = int(getattr(usage, "prompt_tokens", 0) or 0)
        try:
            traces.log_call({"model": self.model, "in": tokens, "out": 0, "cached": 0})
        except Exception:  # noqa: BLE001 — трассировка не меняет результат провайдера
            pass
