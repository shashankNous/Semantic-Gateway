import os
import asyncio
import logging

import asyncpg

from app.env import load_env

# Before the app imports below: app.services.embeddings reads its settings at
# import time, and this module is the first to load .env.
load_env()

from app.security import SCOPE_DASHBOARD, SCOPE_PROXY, hash_token  # noqa: E402
from app.services.embeddings import EMBEDDING_DIM  # noqa: E402

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


async def verify_auth_schema():
    """Fail fast if the credential backfill has not run against this database.

    `get_tenant_by_token` matches on the hash columns and has no plaintext
    fallback, so an app pointed at a database where `scripts/migrate_hash_keys.py`
    has not finished answers *every* request with a 401. At the caller that is
    indistinguishable from a bad key, and on the server it is a stream of ordinary
    401s rather than an error. One check at startup turns that into a single loud
    failure naming the step that was skipped.

    Refuses to start on:

      * the hash columns not existing (migration 005 not applied), or
      * a tenant with a NULL `api_key_hash` (005 applied, backfill not run) — that
        tenant cannot authenticate to the proxy at all.

    Warns but starts on a NULL `dashboard_token_hash`: only that tenant's
    `/v1/stats*` reads are affected, its proxy traffic still works, and taking the
    whole gateway down over a missing dashboard token is out of proportion.

    An empty `tenants` table is not an error — a fresh install has nothing to
    backfill yet, and tenants are only ever inserted with hashes.
    """
    if pool is None:
        raise RuntimeError("Postgres pool is not initialized")

    async with pool.acquire() as conn:
        try:
            row = await conn.fetchrow(
                """
                SELECT count(*) AS total,
                       count(*) FILTER (WHERE api_key_hash IS NULL) AS missing_proxy,
                       count(*) FILTER (WHERE dashboard_token_hash IS NULL) AS missing_dashboard
                FROM tenants
                """
            )
        # ASCII only in these messages: they surface in server logs, where a
        # cp1252 console renders an em dash as a replacement character.
        except asyncpg.UndefinedTableError as exc:
            raise RuntimeError(
                "No `tenants` table in this database. Apply migrations/ in order "
                "before starting the app (see README)."
            ) from exc
        except asyncpg.UndefinedColumnError as exc:
            raise RuntimeError(
                "tenants.api_key_hash / tenants.dashboard_token_hash are missing - "
                "apply migrations/005_token_scopes.sql, then run "
                "scripts/migrate_hash_keys.py --mode=in-place to backfill, then "
                "start the app. Authentication has no plaintext fallback, so "
                "starting now would 401 every request."
            ) from exc

    if row["missing_proxy"]:
        raise RuntimeError(
            "%s of %s tenant(s) have no tenants.api_key_hash - the credential "
            "backfill has not finished. Run scripts/migrate_hash_keys.py "
            "--mode=in-place to completion before starting the app; until it has, "
            "every proxy request from those tenants returns 401."
            % (row["missing_proxy"], row["total"])
        )

    if row["missing_dashboard"]:
        logger.warning(
            "%s of %s tenant(s) have no tenants.dashboard_token_hash; their "
            "/v1/stats* calls will return 401. Run scripts/migrate_hash_keys.py "
            "--mode=in-place to issue one.",
            row["missing_dashboard"],
            row["total"],
        )

    logger.info("Credential schema verified: %s tenant(s), all with a proxy key hash",
                row["total"])


async def verify_embedding_dimension(expected: int | None = None):
    """Fail fast if `cache_entries.embedding` is not as wide as EMBEDDING_DIM.

    Otherwise the first cache miss after a model or dimension change embeds fine,
    calls upstream, and then fails on the INSERT with a pgvector dimension error,
    after the upstream call has already been paid for.
    """
    if pool is None:
        raise RuntimeError("Postgres pool is not initialized")

    expected = EMBEDDING_DIM if expected is None else expected

    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT to_regclass('cache_entries') IS NOT NULL AS has_table,
                   (SELECT atttypmod FROM pg_attribute
                    WHERE attrelid = to_regclass('cache_entries')
                      AND attname = 'embedding'
                      AND NOT attisdropped) AS dim
            """
        )

    # ASCII only, as above.
    if not row["has_table"]:
        raise RuntimeError(
            "No `cache_entries` table in this database. Apply migrations/ in order "
            "before starting the app (see README)."
        )

    actual = row["dim"]
    if actual != expected:
        raise RuntimeError(
            "cache_entries.embedding is vector(%s) but EMBEDDING_DIM is %s - the "
            "configured embedding model does not fit this database. Point the app "
            "at a database migrated with EMBEDDING_DIM=%s, or set EMBEDDING_DIM "
            "(and EMBEDDING_MODEL) to match the column."
            % (actual, expected, expected)
        )

    logger.info("Embedding column verified: vector(%s)", actual)


#: Which hash column backs each scope. See migrations/005_token_scopes.sql for why
#: scope is a column rather than a row-per-token.
_SCOPE_COLUMNS = {
    SCOPE_PROXY: "api_key_hash",
    SCOPE_DASHBOARD: "dashboard_token_hash",
}


async def get_tenant_by_token(token: str, scope: str):
    """Resolve a bearer token to a tenant, scoped to proxy or dashboard use.

    The token is hashed before it reaches SQL — the plaintext is never compared
    against, and never stored. Returns None if no tenant holds that token *in
    that scope*, so a dashboard token presented to the proxy route looks exactly
    like an unknown credential.
    """
    if pool is None:
        raise RuntimeError("Postgres pool is not initialized")

    column = _SCOPE_COLUMNS.get(scope)
    if column is None:
        raise ValueError(f"Unknown token scope: {scope!r}")

    async with pool.acquire() as conn:
        try:
            return await conn.fetchrow(
                f"SELECT id, uuid, name FROM tenants WHERE {column} = $1",
                hash_token(token),
            )
        except asyncpg.UndefinedColumnError as exc:
            # Failing closed here is the point: without the hash columns there is
            # no safe way to authenticate, and silently falling back to the
            # plaintext `api_key` column would undo the migration.
            # ASCII only: this surfaces in server logs, where a cp1252 console
            # renders an em dash as a replacement character.
            raise RuntimeError(
                "tenants.%s is missing - apply migrations/005_token_scopes.sql and "
                "run scripts/migrate_hash_keys.py" % column
            ) from exc


async def log_stats_access(
    tenant_id: int | None,
    endpoint: str,
    requester_ip: str | None,
    status_code: int,
):
    """Record one /v1/stats* call. Best-effort: never fails the request."""
    if pool is None:
        return

    try:
        async with pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO stats_access_log (tenant_id, endpoint, requester_ip, status_code)
                VALUES ($1, $2, $3, $4)
                """,
                tenant_id,
                endpoint,
                requester_ip,
                status_code,
            )
    except Exception:
        logger.exception("Could not write stats_access_log row for %s", endpoint)


async def log_request(
    tenant_id: int,
    prompt_hash: str,
    hit: bool,
    latency_ms: int,
    model: str,
    prompt_tokens: int | None,
    completion_tokens: int | None,
    cache_status: str | None = None,
    similarity: float | None = None,
):
    if pool is None:
        raise RuntimeError("Postgres pool is not initialized")

    async with pool.acquire() as conn:
        try:
            await conn.execute(
                """
                INSERT INTO requests (
                    tenant_id,
                    prompt_hash,
                    hit,
                    latency_ms,
                    model,
                    prompt_tokens,
                    completion_tokens,
                    cache_status,
                    similarity
                )
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9)
                """,
                tenant_id,
                prompt_hash,
                hit,
                latency_ms,
                model,
                prompt_tokens,
                completion_tokens,
                cache_status,
                similarity,
            )
        except asyncpg.UndefinedColumnError:
            await conn.execute(
                """
                INSERT INTO requests (tenant_id, prompt_hash, hit, latency_ms, model, prompt_tokens, completion_tokens)
                VALUES ($1, $2, $3, $4, $5, $6, $7)
                """,
                tenant_id,
                prompt_hash,
                hit,
                latency_ms,
                model,
                prompt_tokens,
                completion_tokens,
            )
