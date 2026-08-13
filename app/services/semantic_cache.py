import json
import os
from typing import Any

import app.db as db
from app.services.match_verification import has_entity_mismatch, has_polarity_mismatch


DEFAULT_THRESHOLD = float(os.getenv("SEMANTIC_CACHE_THRESHOLD", "0.92"))


def _embedding_to_pgvector(embedding: list[float]) -> str:
    return "[" + ",".join(str(value) for value in embedding) + "]"


def _decode_response_json(value: Any) -> dict:
    if isinstance(value, str):
        return json.loads(value)
    return value


def _is_unsafe_match(query: str | None, candidate_query: str | None) -> str | None:
    """Return the name of the failed verification check, or None if the pair is safe."""
    if not query or not candidate_query:
        return None

    if has_polarity_mismatch(query, candidate_query):
        return "polarity mismatch"

    if has_entity_mismatch(query, candidate_query):
        return "entity mismatch"

    return None


async def find_semantic_match(
    tenant_id,
    embedding: list[float],
    query: str | None = None,
    threshold: float = DEFAULT_THRESHOLD,
    raw_query: str | None = None,
):
    if db.pool is None:
        raise RuntimeError("Postgres pool is not initialized")

    embedding_value = _embedding_to_pgvector(embedding)
    query_to_verify = raw_query or query

    async with db.pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT id, response_json, normalized_query, raw_query,
                   1 - (embedding <=> $2::vector) AS similarity
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
                # Rows stored before migration 003 have no raw_query; fall back to the
                # normalized form, which still supports the polarity and number checks.
                candidate_query = row.get("raw_query") or row["normalized_query"]
                failed_check = _is_unsafe_match(query_to_verify, candidate_query)
                if failed_check is not None:
                    print(
                        f"Skipped unsafe semantic match ({failed_check}): "
                        f"{query_to_verify!r} vs {candidate_query!r} similarity={similarity}"
                    )
                    continue

                await conn.execute(
                    """
                    UPDATE cache_entries
                    SET hit_count = hit_count + 1,
                        min_similarity = LEAST(COALESCE(min_similarity, $2), $2),
                        max_similarity = GREATEST(COALESCE(max_similarity, $2), $2)
                    WHERE id = $1::uuid
                    """,
                    str(row["id"]),
                    similarity,
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
    raw_query: str | None = None,
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
                model,
                raw_query
            )
            VALUES (gen_random_uuid(), $1::uuid, $2, $3, $4::jsonb, $5::vector, $6, $7)
            """,
            str(tenant_id),
            normalized_query,
            prompt_hash,
            json.dumps(response_json),
            _embedding_to_pgvector(embedding),
            model,
            raw_query,
        )
