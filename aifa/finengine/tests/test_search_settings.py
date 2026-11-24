from pathlib import Path

import pytest

from finengine.config import Settings

_SEARCH_ENV = (
    "FINENGINE_OPENSEARCH_URL",
    "FINENGINE_EMBEDDING_PROVIDER",
    "FINENGINE_EMBEDDING_MODEL",
    "FINENGINE_EMBEDDING_BATCH_SIZE",
)


def test_search_settings_have_safe_defaults(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    for name in _SEARCH_ENV:
        monkeypatch.delenv(name, raising=False)

    settings = Settings(
        _env_file=None,
        data_dir=tmp_path,
        docs_dir=tmp_path / "docs",
    )

    assert settings.opensearch_url == ""
    assert settings.embedding_provider == "gigachat"
    assert settings.embedding_model == "Embeddings"
    assert settings.embedding_batch_size == 32
