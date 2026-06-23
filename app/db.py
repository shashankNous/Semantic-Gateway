import os
import asyncio
import logging

from dotenv import load_dotenv
import asyncpg

load_dotenv()

logger = logging.getLogger(__name__)

pool: asyncpg.Pool | None = None


def get_database_url() -> str:
    database_url = os.getenv("DATABASE_URL")
    if not database_url:
        raise RuntimeError("DATABASE_URL is not configured; set it on the Railway FastAPI service")
    return database_url


async def init_db_pool():
    global pool

    database_url = get_database_url()
    last_error: Exception | None = None

    for attempt in range(1, 6):
        try:
            pool = await asyncpg.create_pool(database_url, min_size=1, max_size=10)
            logger.info("Postgres connection pool initialized")
            return
        except Exception as exc:
            last_error = exc
            logger.exception("Postgres connection attempt %s/5 failed", attempt)
            if attempt < 5:
                await asyncio.sleep(2)

    raise RuntimeError("Could not connect to Postgres after 5 attempts") from last_error


async def close_db_pool():
    if pool:
        await pool.close()


async def get_tenant_by_key(api_key: str):
    if pool is None:
        raise RuntimeError("Postgres pool is not initialized")

    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT id, name FROM tenants WHERE api_key = $1",
            api_key,
        )
        return row  # None if not found


async def log_request(
    tenant_id: int,
    prompt_hash: str,
    hit: bool,
    latency_ms: int,
    model: str,
    prompt_tokens: int | None,
    completion_tokens: int | None,
):
    if pool is None:
        raise RuntimeError("Postgres pool is not initialized")

    async with pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO requests (tenant_id, prompt_hash, hit, latency_ms, model, prompt_tokens, completion_tokens)
            VALUES ($1, $2, $3, $4, $5, $6, $7)
            """,
            tenant_id, prompt_hash, hit, latency_ms, model, prompt_tokens, completion_tokens,
        )
