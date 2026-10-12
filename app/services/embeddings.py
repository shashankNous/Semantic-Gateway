"""Query embedding, behind one provider-agnostic call.

Every provider here speaks the OpenAI `/embeddings` request and response shape, so
a provider is only a set of defaults. Settings, all read once at import:

  * `EMBEDDING_PROVIDER` (optional) picks a preset of defaults:

      ==========  ==========================  ===============  ======================  ======
      preset      base URL                    API key from     model                   native
      ==========  ==========================  ===============  ======================  ======
      `openai`    https://api.openai.com/v1   OPENAI_API_KEY   text-embedding-3-small  1536
      `mistral`   https://api.mistral.ai/v1   MISTRAL_API_KEY  mistral-embed           1024
      ==========  ==========================  ===============  ======================  ======

    Unset means `openai`, the production setting. Any other name stops the app at
    startup.
  * `EMBEDDING_BASE_URL`, `EMBEDDING_API_KEY`, `EMBEDDING_MODEL` and
    `EMBEDDING_NATIVE_DIM` override the preset one by one. This is how local
    testing points the `openai` shape at Ollama.
  * Two widths, deliberately separate:
      - `EMBEDDING_DIM` is the width of the `cache_entries.embedding` column
        (default 1536). Migrations are rendered with it (scripts/_migrations.py)
        and app/db.py:verify_embedding_dimension checks the database agrees.
      - `EMBEDDING_NATIVE_DIM` is how many numbers the model returns: explicit,
        else the preset's, else `EMBEDDING_DIM`. It may be narrower than the
        column, never wider. Every response is checked against it, then
        zero-padded up to the column (see `_pad_to_column_width`).

**Never mix embedding models within one tenant's `cache_entries`.** Vectors from
different models are not comparable even when they fit the same column: a padded
1024-wide `mistral-embed` vector and a native 1536-wide OpenAI one will happily
produce a cosine score, and it means nothing. Nothing in the code can catch this,
because each row's vector looks valid on its own. `find_semantic_match` only
compares within one tenant, so changing provider or model means a fresh tenant, or
clearing that tenant's `cache_entries` first, not just editing `.env`.
"""

import math
import os
from collections.abc import Mapping
from typing import Any

import httpx


DEFAULT_EMBEDDING_DIM = 1536

OPENAI_EMBEDDINGS_URL = "https://api.openai.com/v1/embeddings"
MISTRAL_EMBEDDINGS_URL = "https://api.mistral.ai/v1/embeddings"

#: Defaults each preset fills in. Only defaults: every one of these has a generic
#: EMBEDDING_* variable that overrides it.
PROVIDER_PRESETS = {
    "openai": {
        "base_url": "https://api.openai.com/v1",
        "api_key_var": "OPENAI_API_KEY",
        "model": "text-embedding-3-small",
        "native_dim": 1536,
    },
    "mistral": {
        "base_url": "https://api.mistral.ai/v1",
        # The same key as the chat-completions upstream: Mistral scopes one API
        # key across all of its endpoints.
        "api_key_var": "MISTRAL_API_KEY",
        "model": "mistral-embed",
        "native_dim": 1024,
    },
}

#: What an unset EMBEDDING_PROVIDER means.
DEFAULT_PROVIDER = "openai"


def parse_timeout_seconds(raw: str) -> float:
    """EMBEDDING_TIMEOUT_SECONDS as a finite number of seconds greater than zero."""
    try:
        value = float(raw)
    except ValueError:
        value = math.nan

    if not math.isfinite(value) or value <= 0:
        # Refuse to guess, as RATE_LIMIT_FAIL_MODE does: a typo silently becoming
        # a default would pick a behaviour the operator did not ask for.
        raise RuntimeError(
            f"EMBEDDING_TIMEOUT_SECONDS must be a number of seconds greater than 0, "
            f"got {raw!r}"
        )
    return value


def _parse_width(name: str, raw: str) -> int:
    try:
        value = int(raw)
    except ValueError:
        value = 0

    if value <= 0:
        raise RuntimeError(f"{name} must be a whole number greater than 0, got {raw!r}")
    return value


def resolve_embedding_config(env: Mapping[str, str]) -> dict[str, Any]:
    """Every embedding setting, resolved from `env`. Raises on anything invalid.

    Called once at import with os.environ, which is what makes a bad setting stop
    the app at startup rather than fail on the first request. Returns the module
    settings by name.
    """
    if "EMBEDDING_DIMS" in env:
        # The wip design's name for the column width. Honouring it silently, or
        # ignoring it silently, would both leave someone's width wrong.
        raise RuntimeError(
            "EMBEDDING_DIMS was renamed to EMBEDDING_DIM (the width of the "
            "cache_entries.embedding column). Set EMBEDDING_DIM instead; for a model "
            "narrower than the column, also set EMBEDDING_NATIVE_DIM."
        )

    provider = (env.get("EMBEDDING_PROVIDER") or DEFAULT_PROVIDER).strip().lower()
    preset = PROVIDER_PRESETS.get(provider)
    if preset is None:
        raise RuntimeError(
            f"Unsupported embedding provider {provider!r} in EMBEDDING_PROVIDER. "
            f"Valid names: {', '.join(sorted(PROVIDER_PRESETS))}. Leave it unset for "
            f"{DEFAULT_PROVIDER}, or point EMBEDDING_BASE_URL at any OpenAI-compatible "
            "endpoint."
        )

    column_dim = _parse_width("EMBEDDING_DIM", env.get("EMBEDDING_DIM") or str(DEFAULT_EMBEDDING_DIM))

    model = env.get("EMBEDDING_MODEL") or preset["model"]
    if env.get("EMBEDDING_NATIVE_DIM"):
        native_dim = _parse_width("EMBEDDING_NATIVE_DIM", env["EMBEDDING_NATIVE_DIM"])
        native_source = "EMBEDDING_NATIVE_DIM"
    elif env.get("EMBEDDING_PROVIDER"):
        native_dim = preset["native_dim"]
        native_source = f"the {provider} preset"
    else:
        # The generic setting: no preset was asked for, so the model is assumed to
        # fill the column exactly, as before presets existed.
        native_dim = column_dim
        native_source = "EMBEDDING_DIM"

    if native_dim > column_dim:
        raise RuntimeError(
            f"Model {model!r} returns {native_dim} numbers (from {native_source}), which "
            f"is wider than the cache_entries.embedding column (EMBEDDING_DIM="
            f"{column_dim}). Vectors are padded up to the column, never truncated: "
            "raise EMBEDDING_DIM and migrate a matching database, or use a narrower model."
        )

    base_url = (env.get("EMBEDDING_BASE_URL") or preset["base_url"]).rstrip("/")

    return {
        "EMBEDDING_PROVIDER": provider,
        "EMBEDDING_BASE_URL": base_url,
        "EMBEDDINGS_URL": f"{base_url}/embeddings",
        "EMBEDDING_API_KEY_VAR": preset["api_key_var"],
        "EMBEDDING_MODEL": model,
        "EMBEDDING_DIM": column_dim,
        "EMBEDDING_NATIVE_DIM": native_dim,
        "EMBEDDING_TIMEOUT_SECONDS": parse_timeout_seconds(
            env.get("EMBEDDING_TIMEOUT_SECONDS") or "10"
        ),
    }


_CONFIG = resolve_embedding_config(os.environ)

EMBEDDING_PROVIDER: str = _CONFIG["EMBEDDING_PROVIDER"]
EMBEDDING_BASE_URL: str = _CONFIG["EMBEDDING_BASE_URL"]
EMBEDDINGS_URL: str = _CONFIG["EMBEDDINGS_URL"]
#: The variable the API key is read from when EMBEDDING_API_KEY is unset. Read per
#: request rather than here, so the key never sits in a module global.
EMBEDDING_API_KEY_VAR: str = _CONFIG["EMBEDDING_API_KEY_VAR"]
EMBEDDING_MODEL: str = _CONFIG["EMBEDDING_MODEL"]
#: Width of `cache_entries.embedding`.
EMBEDDING_DIM: int = _CONFIG["EMBEDDING_DIM"]
#: Width the model returns; <= EMBEDDING_DIM.
EMBEDDING_NATIVE_DIM: int = _CONFIG["EMBEDDING_NATIVE_DIM"]
#: How long one embedding call may take before the request degrades to a plain
#: miss (see EmbeddingUnavailableError). Every exact-cache miss waits this long
#: when the embedding service hangs, so it is kept short; the first call to a
#: local Ollama model needs more while the model loads (.env.local.example).
EMBEDDING_TIMEOUT_SECONDS: float = _CONFIG["EMBEDDING_TIMEOUT_SECONDS"]


class EmbeddingUnavailableError(RuntimeError):
    """No embedding could be produced for this request, for any reason.

    One type for every failure (unreachable endpoint, timeout, HTTP error such as
    Ollama's 404 for an unknown model, malformed or wrong-size response, missing
    key), so the proxy can degrade to a plain miss without knowing which provider
    is behind this. Messages name the failure and the model; never the key or the
    input. A RuntimeError, so code that treated the old configuration errors as
    RuntimeError still does.
    """


def _pad_to_column_width(embedding: list[float]) -> list[float]:
    """Right-pad a narrower embedding with zeros to EMBEDDING_DIM.

    `mistral-embed` returns 1024 dimensions and has no option to return more, but
    the column is `vector(1536)`. Padding rather than migrating the column keeps
    every existing 1536-dimension row readable, which an
    `ALTER COLUMN ... TYPE vector(1024)` would not.

    This is safe because cosine similarity is exactly invariant under appending
    the same zeros to both vectors: the dot product gains only 0*0 terms and
    neither norm changes. Scores are therefore the model's true scores, not an
    approximation, and the HNSW `vector_cosine_ops` index is unaffected.

    The rule it depends on is the module's: never mix embedding models within one
    tenant. A padded vector and a native-width one both fit the column but are not
    comparable to each other.
    """
    if len(embedding) == EMBEDDING_DIM:
        return embedding

    if len(embedding) > EMBEDDING_DIM:
        # Truncating would silently change what the vector means, and the insert
        # would succeed, so this has to be loud. embed_text() checks the native
        # width first, so reaching this means the settings themselves disagree.
        raise EmbeddingUnavailableError(
            f"{EMBEDDING_MODEL!r} returned {len(embedding)} dimensions, which does not "
            f"fit the vector({EMBEDDING_DIM}) column."
        )

    return embedding + [0.0] * (EMBEDDING_DIM - len(embedding))


async def embed_text(text: str) -> list[float]:
    # EMBEDDING_API_KEY overrides the preset's key variable, so local testing can
    # use a dummy key without touching OPENAI_API_KEY or MISTRAL_API_KEY.
    api_key = os.getenv("EMBEDDING_API_KEY") or os.getenv(EMBEDDING_API_KEY_VAR)
    if not api_key:
        raise EmbeddingUnavailableError(
            f"{EMBEDDING_API_KEY_VAR} is not configured: no embedding API key for "
            f"{EMBEDDING_PROVIDER} (set {EMBEDDING_API_KEY_VAR}, or EMBEDDING_API_KEY "
            "to override)"
        )

    embedding = await _post_embedding(EMBEDDINGS_URL, api_key, text)

    # The startup check only proves the column matches EMBEDDING_DIM; this proves
    # the model returns what the settings say. Otherwise a mismatch surfaces at the
    # cache_entries INSERT, after the upstream completion has already been paid
    # for, or, for a narrower model, gets padded into a meaningless vector.
    if len(embedding) != EMBEDDING_NATIVE_DIM:
        raise EmbeddingUnavailableError(
            f"Embedding has the wrong size: expected {EMBEDDING_NATIVE_DIM}, got "
            f"{len(embedding)} from model {EMBEDDING_MODEL!r}. Set EMBEDDING_NATIVE_DIM "
            "(or EMBEDDING_DIM, if the model fills the column) to match the model."
        )

    return _pad_to_column_width(embedding)


async def _post_embedding(url: str, api_key: str, text: str) -> list[float]:
    """One embeddings round trip. Every provider speaks the same request/response shape."""
    # httpx.HTTPError covers connection errors, timeouts and, via
    # raise_for_status(), non-2xx responses. asyncio.CancelledError is not an
    # httpx error and propagates untouched.
    try:
        async with httpx.AsyncClient(timeout=EMBEDDING_TIMEOUT_SECONDS) as client:
            response = await client.post(
                url,
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "Content-Type": "application/json",
                },
                json={
                    "model": EMBEDDING_MODEL,
                    "input": text,
                },
            )
            response.raise_for_status()
    except httpx.HTTPStatusError as exc:
        raise EmbeddingUnavailableError(
            f"Embedding endpoint returned HTTP {exc.response.status_code} "
            f"for model {EMBEDDING_MODEL!r}"
        ) from exc
    except httpx.HTTPError as exc:
        raise EmbeddingUnavailableError(
            f"Embedding request failed ({type(exc).__name__}) for model {EMBEDDING_MODEL!r}"
        ) from exc

    try:
        data: dict[str, Any] = response.json()
        embedding = data["data"][0]["embedding"]
    except (ValueError, KeyError, IndexError, TypeError) as exc:
        raise EmbeddingUnavailableError(
            f"Malformed embedding response ({type(exc).__name__}) for model {EMBEDDING_MODEL!r}"
        ) from exc

    if not isinstance(embedding, list):
        raise EmbeddingUnavailableError(
            f"Malformed embedding response (embedding is {type(embedding).__name__}, "
            f"not a list) for model {EMBEDDING_MODEL!r}"
        )

    return embedding
