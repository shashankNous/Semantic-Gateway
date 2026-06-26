import os
from typing import Any

import httpx


EMBEDDING_PROVIDER = os.getenv("EMBEDDING_PROVIDER", "openai").lower()
EMBEDDING_MODEL = os.getenv("EMBEDDING_MODEL", "text-embedding-3-small")
OPENAI_EMBEDDINGS_URL = "https://api.openai.com/v1/embeddings"


async def embed_text(text: str) -> list[float]:
    if EMBEDDING_PROVIDER == "openai":
        return await _embed_with_openai(text)

    raise RuntimeError(f"Unsupported embedding provider: {EMBEDDING_PROVIDER}")


async def _embed_with_openai(text: str) -> list[float]:
    api_key = os.getenv("OPENAI_API_KEY")
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
