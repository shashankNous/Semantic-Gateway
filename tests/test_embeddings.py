"""The embedding provider dispatch, and the padding that lets a 1024-dimension
model write into a vector(1536) column.

Nothing here makes a network call: `_post_embedding` is monkeypatched, so these
cover the dispatch, the key guards and the padding arithmetic only.
"""

import asyncio
import math
import os

import pytest


def run(coro):
    return asyncio.run(coro)


EMBEDDING_ENV = (
    "EMBEDDING_PROVIDER", "EMBEDDING_BASE_URL", "EMBEDDING_API_KEY", "EMBEDDING_MODEL",
    "EMBEDDING_DIM", "EMBEDDING_NATIVE_DIM", "EMBEDDING_DIMS", "EMBEDDING_TIMEOUT_SECONDS",
    "OPENAI_API_KEY", "MISTRAL_API_KEY",
)


def reload_embeddings(monkeypatch, **env):
    """Configure the module as if it had been imported under a given environment.

    Settings are resolved at import time, so setting an env var after the fact
    would not change them. This resolves them the way import does
    (`resolve_embedding_config`) and patches the module's settings, rather than
    `importlib.reload`: a reload replaces `EmbeddingUnavailableError` with a new
    class, and the proxy route's `except` clause, bound to the old one, would stop
    catching what embed_text() raises for every test that runs afterwards.
    """
    for key in EMBEDDING_ENV:
        monkeypatch.delenv(key, raising=False)
    for key, value in env.items():
        monkeypatch.setenv(key, value)

    import app.services.embeddings as embeddings
    for name, value in embeddings.resolve_embedding_config(os.environ).items():
        monkeypatch.setattr(embeddings, name, value)
    return embeddings


def cosine(a, b):
    dot = sum(x * y for x, y in zip(a, b))
    norm = math.sqrt(sum(x * x for x in a)) * math.sqrt(sum(y * y for y in b))
    return dot / norm


def test_mistral_default_model(monkeypatch):
    embeddings = reload_embeddings(monkeypatch, EMBEDDING_PROVIDER="mistral")
    assert embeddings.EMBEDDING_MODEL == "mistral-embed"


def test_openai_default_model(monkeypatch):
    embeddings = reload_embeddings(monkeypatch, EMBEDDING_PROVIDER="openai")
    assert embeddings.EMBEDDING_MODEL == "text-embedding-3-small"


def test_explicit_model_overrides_the_provider_default(monkeypatch):
    embeddings = reload_embeddings(
        monkeypatch, EMBEDDING_PROVIDER="mistral", EMBEDDING_MODEL="codestral-embed"
    )
    assert embeddings.EMBEDDING_MODEL == "codestral-embed"


def test_mistral_embedding_is_padded_to_column_width(monkeypatch):
    embeddings = reload_embeddings(
        monkeypatch, EMBEDDING_PROVIDER="mistral", MISTRAL_API_KEY="test-key"
    )

    captured = {}

    async def fake_post(url, api_key, text):
        captured["url"] = url
        captured["api_key"] = api_key
        return [0.5] * 1024

    monkeypatch.setattr(embeddings, "_post_embedding", fake_post)

    result = run(embeddings.embed_text("how do i pay"))

    assert captured["url"] == embeddings.MISTRAL_EMBEDDINGS_URL
    assert captured["api_key"] == "test-key"
    assert len(result) == 1536
    assert result[:1024] == [0.5] * 1024
    # The tail is padding, not data.
    assert result[1024:] == [0.0] * 512


def test_padding_preserves_cosine_similarity(monkeypatch):
    """The reason padding is safe at all: scores are unchanged by the zero tail."""
    embeddings = reload_embeddings(monkeypatch, EMBEDDING_PROVIDER="mistral")

    raw_a = [0.1 * (i % 7) + 0.01 for i in range(1024)]
    raw_b = [0.1 * (i % 5) + 0.02 for i in range(1024)]

    padded_a = embeddings._pad_to_column_width(list(raw_a))
    padded_b = embeddings._pad_to_column_width(list(raw_b))

    assert cosine(padded_a, padded_b) == pytest.approx(cosine(raw_a, raw_b), rel=1e-12)


def test_native_width_embedding_is_untouched(monkeypatch):
    embeddings = reload_embeddings(
        monkeypatch, EMBEDDING_PROVIDER="openai", OPENAI_API_KEY="test-key"
    )

    async def fake_post(url, api_key, text):
        return [0.25] * 1536

    monkeypatch.setattr(embeddings, "_post_embedding", fake_post)

    result = run(embeddings.embed_text("how do i pay"))
    assert result == [0.25] * 1536


def test_oversized_embedding_is_refused_not_truncated(monkeypatch):
    embeddings = reload_embeddings(monkeypatch, EMBEDDING_PROVIDER="mistral")

    with pytest.raises(RuntimeError, match="does not fit the vector"):
        embeddings._pad_to_column_width([0.1] * 3072)


def test_missing_mistral_key_is_refused(monkeypatch):
    embeddings = reload_embeddings(monkeypatch, EMBEDDING_PROVIDER="mistral")

    with pytest.raises(RuntimeError, match="MISTRAL_API_KEY is not configured"):
        run(embeddings.embed_text("how do i pay"))


def test_missing_openai_key_is_refused(monkeypatch):
    embeddings = reload_embeddings(monkeypatch, EMBEDDING_PROVIDER="openai")

    with pytest.raises(RuntimeError, match="OPENAI_API_KEY is not configured"):
        run(embeddings.embed_text("how do i pay"))


def test_unknown_provider_is_refused(monkeypatch):
    # Refused while resolving settings, i.e. at startup, not on the first request.
    with pytest.raises(RuntimeError, match="Unsupported embedding provider"):
        reload_embeddings(monkeypatch, EMBEDDING_PROVIDER="cohere")


# --- Presets over the generic EMBEDDING_* settings -------------------------------
# resolve_embedding_config() is what import runs; calling it directly needs no
# module state at all.

from app.services.embeddings import resolve_embedding_config  # noqa: E402


def test_no_provider_resolves_to_the_production_openai_settings():
    config = resolve_embedding_config({})

    assert config["EMBEDDINGS_URL"] == "https://api.openai.com/v1/embeddings"
    assert config["EMBEDDING_API_KEY_VAR"] == "OPENAI_API_KEY"
    assert config["EMBEDDING_MODEL"] == "text-embedding-3-small"
    assert config["EMBEDDING_DIM"] == 1536
    assert config["EMBEDDING_NATIVE_DIM"] == 1536
    assert config["EMBEDDING_TIMEOUT_SECONDS"] == 10.0


def test_the_openai_preset_is_the_production_settings():
    assert resolve_embedding_config({"EMBEDDING_PROVIDER": "openai"}) == resolve_embedding_config({})


def test_the_mistral_preset_fills_in_mistral_defaults():
    config = resolve_embedding_config({"EMBEDDING_PROVIDER": "mistral"})

    assert config["EMBEDDINGS_URL"] == "https://api.mistral.ai/v1/embeddings"
    assert config["EMBEDDING_API_KEY_VAR"] == "MISTRAL_API_KEY"
    assert config["EMBEDDING_MODEL"] == "mistral-embed"
    assert config["EMBEDDING_NATIVE_DIM"] == 1024
    # The column keeps its production width; the model is padded up to it.
    assert config["EMBEDDING_DIM"] == 1536


def test_the_provider_name_is_case_insensitive():
    assert resolve_embedding_config({"EMBEDDING_PROVIDER": "Mistral"})["EMBEDDING_MODEL"] == "mistral-embed"


def test_explicit_generic_settings_override_the_preset():
    config = resolve_embedding_config({
        "EMBEDDING_PROVIDER": "mistral",
        "EMBEDDING_BASE_URL": "http://localhost:11434/v1/",
        "EMBEDDING_MODEL": "nomic-embed-text",
        "EMBEDDING_NATIVE_DIM": "768",
    })

    assert config["EMBEDDINGS_URL"] == "http://localhost:11434/v1/embeddings"
    assert config["EMBEDDING_MODEL"] == "nomic-embed-text"
    assert config["EMBEDDING_NATIVE_DIM"] == 768


def test_without_a_preset_the_native_width_defaults_to_the_column():
    """The local Ollama config: no provider, EMBEDDING_DIM=768 for both."""
    config = resolve_embedding_config({"EMBEDDING_DIM": "768"})

    assert config["EMBEDDING_NATIVE_DIM"] == 768


def test_a_native_width_narrower_than_the_column_is_allowed():
    config = resolve_embedding_config({"EMBEDDING_DIM": "1536", "EMBEDDING_NATIVE_DIM": "768"})

    assert (config["EMBEDDING_DIM"], config["EMBEDDING_NATIVE_DIM"]) == (1536, 768)


def test_an_unsupported_provider_lists_the_valid_names():
    with pytest.raises(RuntimeError) as excinfo:
        resolve_embedding_config({"EMBEDDING_PROVIDER": "cohere"})

    message = str(excinfo.value)
    assert "'cohere'" in message
    assert "mistral" in message and "openai" in message


def test_the_old_embedding_dims_name_is_refused_with_the_rename():
    """Not silently honoured or ignored: either would leave the width wrong."""
    with pytest.raises(RuntimeError) as excinfo:
        resolve_embedding_config({"EMBEDDING_DIMS": "1536"})

    message = str(excinfo.value)
    assert "EMBEDDING_DIMS" in message
    assert "renamed to EMBEDDING_DIM" in message


@pytest.mark.parametrize(
    "env",
    [
        {"EMBEDDING_NATIVE_DIM": "3072"},
        {"EMBEDDING_DIM": "768", "EMBEDDING_PROVIDER": "mistral"},  # 1024 > 768
    ],
    ids=["explicit-native-too-wide", "preset-native-too-wide"],
)
def test_a_native_width_wider_than_the_column_is_refused(env):
    with pytest.raises(RuntimeError, match="wider than the cache_entries.embedding column"):
        resolve_embedding_config(env)


@pytest.mark.parametrize("name", ["EMBEDDING_DIM", "EMBEDDING_NATIVE_DIM"])
@pytest.mark.parametrize("value", ["0", "-3", "abc"])
def test_dimensions_must_be_positive_integers(name, value):
    with pytest.raises(RuntimeError) as excinfo:
        resolve_embedding_config({name: value})

    assert name in str(excinfo.value)
    assert repr(value) in str(excinfo.value)


@pytest.mark.parametrize(
    "env, expected",
    [
        ({"EMBEDDING_PROVIDER": "cohere"}, "Unsupported embedding provider"),
        ({"EMBEDDING_DIMS": "1024"}, "renamed to EMBEDDING_DIM"),
        ({"EMBEDDING_NATIVE_DIM": "2048"}, "wider than the cache_entries.embedding column"),
    ],
    ids=["unknown-provider", "old-dims-name", "native-too-wide"],
)
def test_bad_embedding_settings_stop_the_app_at_startup(env, expected):
    import pathlib
    import subprocess
    import sys

    clean = {k: v for k, v in os.environ.items() if k not in EMBEDDING_ENV}
    result = subprocess.run(
        [sys.executable, "-c", "import app.main"],
        cwd=pathlib.Path(__file__).resolve().parent.parent,
        env={**clean, **env, "PYTHON_DOTENV_DISABLED": "1"},
        capture_output=True,
        text=True,
    )

    assert result.returncode != 0
    assert expected in result.stderr


# --- Native-width check, then padding --------------------------------------------


def test_padding_a_768_wide_model_preserves_cosine_similarity(monkeypatch):
    """The local Ollama padding config: nomic-embed-text (768) in a 1536 column."""
    embeddings = reload_embeddings(monkeypatch, EMBEDDING_DIM="1536", EMBEDDING_NATIVE_DIM="768")

    raw_a = [math.sin(i * 0.37) for i in range(768)]
    raw_b = [math.cos(i * 0.11) + 0.3 for i in range(768)]

    padded = cosine(embeddings._pad_to_column_width(raw_a), embeddings._pad_to_column_width(raw_b))
    assert padded == pytest.approx(cosine(raw_a, raw_b), abs=1e-9)
