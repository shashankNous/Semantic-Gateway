import json
import os
from typing import Any

import app.db as db


DEFAULT_THRESHOLD = float(os.getenv("SEMANTIC_CACHE_THRESHOLD", "0.92"))


def _embedding_to_pgvector(embedding: list[float]) -> str:
    return "[" + ",".join(str(value) for value in embedding) + "]"


def _decode_response_json(value: Any) -> dict:
    if isinstance(value, str):
        return json.loads(value)
    return value


async def find_semantic_match(
    tenant_id,
    embedding: list[float],
    threshold: float = DEFAULT_THRESHOLD,
):
    if db.pool is None:
        raise RuntimeError("Postgres pool is not initialized")

    embedding_value = _embedding_to_pgvector(embedding)

    async with db.pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT id, response_json, normalized_query, 1 - (embedding <=> $2::vector) AS similarity
            FROM cache_entries
            WHERE tenant_id = $1::uuid
            ORDER BY embedding <=> $2::vector
            LIMIT 5
            """,
            str(tenant_id),
            embedding_value,
        )

        for row in rows:
            similarity = float(row["similarity"])
            if similarity >= threshold:
                await conn.execute(
                    "UPDATE cache_entries SET hit_count = hit_count + 1 WHERE id = $1::uuid",
                    str(row["id"]),
                )
                return {
                    "id": row["id"],
                    "response_json": _decode_response_json(row["response_json"]),
                    "normalized_query": row["normalized_query"],
                    "similarity": similarity,
                }

    return None


async def store_semantic_cache_entry(
    tenant_id,
    normalized_query: str,
    prompt_hash: str,
    response_json: dict,
    embedding: list[float],
    model: str | None = None,
):
    if db.pool is None:
        raise RuntimeError("Postgres pool is not initialized")

    async with db.pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO cache_entries (
                id,
                tenant_id,
                normalized_query,
                prompt_hash,
                response_json,
                embedding,
                model
            )
            VALUES (gen_random_uuid(), $1::uuid, $2, $3, $4::jsonb, $5::vector, $6)
            """,
            str(tenant_id),
            normalized_query,
            prompt_hash,
            json.dumps(response_json),
            _embedding_to_pgvector(embedding),
            model,
        )
