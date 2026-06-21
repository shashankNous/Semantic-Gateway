import os
import asyncpg

DATABASE_URL = os.getenv("DATABASE_URL", "postgresql://user:password@postgres:5432/cachedb")

pool: asyncpg.Pool | None = None


async def init_db_pool():
    global pool
    pool = await asyncpg.create_pool(DATABASE_URL, min_size=1, max_size=10)


async def close_db_pool():
    if pool:
        await pool.close()


async def get_tenant_by_key(api_key: str):
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
    async with pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO requests (tenant_id, prompt_hash, hit, latency_ms, model, prompt_tokens, completion_tokens)
            VALUES ($1, $2, $3, $4, $5, $6, $7)
            """,
            tenant_id, prompt_hash, hit, latency_ms, model, prompt_tokens, completion_tokens,
        )