import os
from typing import Any

import httpx


# "openai" means any OpenAI-compatible /embeddings endpoint, which is how local
# testing points this at Ollama: same provider, different EMBEDDING_BASE_URL.
EMBEDDING_PROVIDER = os.getenv("EMBEDDING_PROVIDER", "openai").lower()
EMBEDDING_MODEL = os.getenv("EMBEDDING_MODEL", "text-embedding-3-small")
EMBEDDING_BASE_URL = os.getenv("EMBEDDING_BASE_URL", "https://api.openai.com/v1").rstrip("/")
OPENAI_EMBEDDINGS_URL = f"{EMBEDDING_BASE_URL}/embeddings"

#: Width of `cache_entries.embedding`. Must match what EMBEDDING_MODEL returns;
#: text-embedding-3-small returns 1536. migrations are rendered with this value
#: (scripts/_migrations.py) and app/db.py:verify_embedding_dimension checks the
#: database agrees at startup.
DEFAULT_EMBEDDING_DIM = 1536
EMBEDDING_DIM = int(os.getenv("EMBEDDING_DIM", str(DEFAULT_EMBEDDING_DIM)))


class EmbeddingUnavailableError(Exception):
    """No embedding could be produced for this request, for any reason.

    One type for every failure (unreachable endpoint, timeout, HTTP error such as
    Ollama's 404 for an unknown model, malformed response, missing key), so the
    proxy can degrade to a plain miss without knowing which provider is behind
    this. Messages name the failure and the model; never the key or the input.
    """


async def embed_text(text: str) -> list[float]:
    if EMBEDDING_PROVIDER == "openai":
        return await _embed_with_openai(text)

    raise RuntimeError(f"Unsupported embedding provider: {EMBEDDING_PROVIDER}")


async def _embed_with_openai(text: str) -> list[float]:
    # EMBEDDING_API_KEY lets the embeddings endpoint use its own credential; unset,
    # it falls back to OPENAI_API_KEY as before.
    api_key = os.getenv("EMBEDDING_API_KEY") or os.getenv("OPENAI_API_KEY")
    if not api_key:
        raise EmbeddingUnavailableError(
            "No embedding API key configured (EMBEDDING_API_KEY or OPENAI_API_KEY)"
        )

    # httpx.HTTPError covers connection errors, timeouts and, via
    # raise_for_status(), non-2xx responses. asyncio.CancelledError is not an
    # httpx error and propagates untouched.
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.post(
                OPENAI_EMBEDDINGS_URL,
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

    # The startup check only proves the column matches EMBEDDING_DIM; this proves
    # the model does. Otherwise a mismatch surfaces at the cache_entries INSERT,
    # after the upstream completion has already been paid for.
    if len(embedding) != EMBEDDING_DIM:
        raise EmbeddingUnavailableError(
            f"Embedding has the wrong size: expected {EMBEDDING_DIM}, got "
            f"{len(embedding)} from model {EMBEDDING_MODEL!r}. Set EMBEDDING_DIM to "
            "match the model (and migrate a matching database)."
        )

    return embedding
