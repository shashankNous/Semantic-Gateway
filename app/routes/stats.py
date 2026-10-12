"""Read-only stats endpoints backing the dashboard.

Every route here derives its tenant from the `dash_...` bearer token via
`require_dashboard_key`. `tenant_id` is deliberately **not** a parameter: when it
was, any caller could read any tenant's numbers by changing an integer.
"""

import os
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException

from app.env import load_env
import app.db as db
from app.auth import require_dashboard_key

load_env()

router = APIRouter()

COST_PER_1K_TOKENS = float(os.getenv("COST_PER_1K_TOKENS", "0.002"))


@router.get("/stats")
async def get_stats(tenant: dict = Depends(require_dashboard_key)):
    if db.pool is None:
        raise HTTPException(status_code=500, detail="Postgres pool is not initialized")

    tenant_id = tenant["tenant_id"]

    async with db.pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT
                COALESCE(cache_status, 'UNKNOWN') AS cache_status,
                COUNT(*) AS count,
                AVG(latency_ms) AS avg_latency_ms,
                COALESCE(SUM(prompt_tokens), 0) AS prompt_tokens,
                COALESCE(SUM(completion_tokens), 0) AS completion_tokens
            FROM requests
            WHERE tenant_id = $1
            GROUP BY COALESCE(cache_status, 'UNKNOWN')
            """,
            tenant_id,
        )

        # Which upstream model actually served this tenant's traffic. Without it a
        # dashboard has no way to price the saved tokens except by guessing, and a
        # guessed model is how you end up showing a dollar figure for a vendor that
        # was never called.
        model_rows = await conn.fetch(
            """
            SELECT COALESCE(model, 'unknown') AS model, COUNT(*) AS count
            FROM requests
            WHERE tenant_id = $1
            GROUP BY COALESCE(model, 'unknown')
            ORDER BY COUNT(*) DESC
            """,
            tenant_id,
        )

    counts = {"EXACT_HIT": 0, "SEMANTIC_HIT": 0, "MISS": 0, "UNCACHEABLE": 0}
    avg_latency_by_cache_status = {}
    total_requests = 0
    prompt_tokens_saved = 0
    completion_tokens_saved = 0

    for row in rows:
        status = row["cache_status"]
        count = row["count"]
        total_requests += count
        if status in counts:
            counts[status] = count
        avg_latency_by_cache_status[status] = (
            float(row["avg_latency_ms"]) if row["avg_latency_ms"] is not None else None
        )
        if status in ("EXACT_HIT", "SEMANTIC_HIT"):
            prompt_tokens_saved += row["prompt_tokens"]
            completion_tokens_saved += row["completion_tokens"]

    hit_count = counts["EXACT_HIT"] + counts["SEMANTIC_HIT"]
    hit_rate = hit_count / total_requests if total_requests else 0.0

    # Prompt and completion tokens are priced differently by every upstream, so the
    # split is what callers actually need to cost this out. estimated_cost_saved_usd
    # stays on the flat COST_PER_1K_TOKENS blend for existing consumers.
    tokens_saved = prompt_tokens_saved + completion_tokens_saved

    return {
        "tenant_id": tenant_id,
        "total_requests": total_requests,
        "exact_hit_count": counts["EXACT_HIT"],
        "semantic_hit_count": counts["SEMANTIC_HIT"],
        "miss_count": counts["MISS"],
        "uncacheable_count": counts["UNCACHEABLE"],
        "hit_rate": round(hit_rate, 4),
        "avg_latency_ms_by_cache_status": avg_latency_by_cache_status,
        "prompt_tokens_saved": prompt_tokens_saved,
        "completion_tokens_saved": completion_tokens_saved,
        "models": [
            {"model": row["model"], "requests": row["count"]} for row in model_rows
        ],
        "estimated_cost_saved_usd": round((tokens_saved / 1000) * COST_PER_1K_TOKENS, 6),
    }


def _format_day(value) -> str:
    """date_trunc gives back a datetime; the API contract is a plain YYYY-MM-DD string."""
    if isinstance(value, str):
        return value[:10]
    if isinstance(value, datetime):
        return value.date().isoformat()
    return value.isoformat()


@router.get("/stats/trend")
async def get_stats_trend(tenant: dict = Depends(require_dashboard_key)):
    if db.pool is None:
        raise HTTPException(status_code=500, detail="Postgres pool is not initialized")

    tenant_id = tenant["tenant_id"]

    async with db.pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT
                date_trunc('day', timestamp) AS day,
                COUNT(*) AS total_requests,
                COUNT(*) FILTER (
                    WHERE cache_status IN ('EXACT_HIT', 'SEMANTIC_HIT')
                ) AS hit_count
            FROM requests
            WHERE tenant_id = $1
              AND timestamp >= date_trunc('day', now()) - INTERVAL '6 days'
            GROUP BY date_trunc('day', timestamp)
            ORDER BY date_trunc('day', timestamp)
            """,
            tenant_id,
        )

    trend = []
    for row in rows:
        total = row["total_requests"]
        hits = row["hit_count"]
        trend.append(
            {
                "date": _format_day(row["day"]),
                "total_requests": total,
                "hit_count": hits,
                "hit_rate": round(hits / total, 4) if total else 0.0,
            }
        )

    return {"tenant_id": tenant_id, "trend": trend}


@router.get("/stats/top-cached")
async def get_top_cached(tenant: dict = Depends(require_dashboard_key)):
    if db.pool is None:
        raise HTTPException(status_code=500, detail="Postgres pool is not initialized")

    tenant_uuid = tenant["tenant_uuid"]

    async with db.pool.acquire() as conn:
        # cache_entries keys on tenants.uuid, which the token lookup already
        # resolved alongside the integer id — no join through tenants needed.
        rows = await conn.fetch(
            """
            SELECT
                normalized_query,
                hit_count,
                min_similarity,
                max_similarity
            FROM cache_entries
            WHERE tenant_id = $1::uuid
            ORDER BY hit_count DESC
            LIMIT 20
            """,
            str(tenant_uuid),
        )

    entries = [
        {
            "normalized_query": row["normalized_query"],
            "hit_count": row["hit_count"] or 0,
            "min_similarity": (
                float(row["min_similarity"]) if row["min_similarity"] is not None else None
            ),
            "max_similarity": (
                float(row["max_similarity"]) if row["max_similarity"] is not None else None
            ),
        }
        for row in rows
    ]

    return {"tenant_id": tenant["tenant_id"], "entries": entries}
