import json
import os

from redis.asyncio import Redis

REDIS_URL = os.getenv("REDIS_URL", "redis://redis:6379/0")
CACHE_TTL_SECONDS = int(os.getenv("CACHE_TTL_SECONDS", "86400"))

redis_client = Redis.from_url(
    REDIS_URL,
    decode_responses=True,
)


async def get_cached_response(key: str):
    cached = await redis_client.get(key)

    if cached is None:
        return None

    return json.loads(cached)


async def set_cached_response(key: str, response: dict):
    await redis_client.setex(
        key,
        CACHE_TTL_SECONDS,
        json.dumps(response),
    )