"""Exact-match response cache, backed by Redis.

Both operations degrade rather than raise when Redis is unreachable: a read
becomes a miss, a write becomes a no-op. A cache that takes the service down
when it is unavailable is not a cache — the request can still be answered by
going upstream, which is exactly what a miss does.

This is what makes `RATE_LIMIT_FAIL_MODE=open` mean anything. The limiter can
decide to let a request through during a Redis outage, but the request only
survives if the layer immediately after it also tolerates Redis being gone.

Note the asymmetry with the rate limiter, which defaults to failing *closed*.
The difference is what each one protects: a cache miss costs one upstream call
and is self-correcting, while a missing rate limit removes the ceiling on how
many upstream calls a single tenant can trigger.
"""

import json
import logging
import os

from redis.asyncio import Redis

logger = logging.getLogger(__name__)

REDIS_URL = os.getenv("REDIS_URL", "redis://redis:6379/0")
CACHE_TTL_SECONDS = int(os.getenv("CACHE_TTL_SECONDS", "86400"))

redis_client = Redis.from_url(
    REDIS_URL,
    decode_responses=True,
)


async def get_cached_response(key: str):
    try:
        cached = await redis_client.get(key)
    except Exception:
        logger.exception("Redis read failed for %s; treating as a cache miss", key)
        return None

    if cached is None:
        return None

    try:
        return json.loads(cached)
    except json.JSONDecodeError:
        # A corrupt entry should not be fatal either; re-fetching replaces it.
        logger.exception("Corrupt cache entry at %s; treating as a cache miss", key)
        return None


async def set_cached_response(key: str, response: dict):
    try:
        await redis_client.setex(
            key,
            CACHE_TTL_SECONDS,
            json.dumps(response),
        )
    except Exception:
        logger.exception("Redis write failed for %s; response not cached", key)
