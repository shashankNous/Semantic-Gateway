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


async def embed_text(text: str) -> list[float]:
    if EMBEDDING_PROVIDER == "openai":
        return await _embed_with_openai(text)

    raise RuntimeError(f"Unsupported embedding provider: {EMBEDDING_PROVIDER}")


async def _embed_with_openai(text: str) -> list[float]:
    # EMBEDDING_API_KEY lets the embeddings endpoint use its own credential; unset,
    # it falls back to OPENAI_API_KEY as before.
    api_key = os.getenv("EMBEDDING_API_KEY") or os.getenv("OPENAI_API_KEY")
    if not api_key:
        raise RuntimeError("OPENAI_API_KEY is not configured")

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

    data: dict[str, Any] = response.json()
    return data["data"][0]["embedding"]
