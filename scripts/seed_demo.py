"""Seed a demo tenant with realistic cache and request history.

Useful for looking at the dashboard (or the /v1/stats* endpoints) without having
to drive real traffic through the gateway first.

    python scripts/seed_demo.py

Reads DATABASE_URL from the environment, falling back to the local demo Postgres
on port 5433. Applies every migration in `migrations/` if they are not already in
place — globbed, not named, so a newly added one is picked up with no change here
(see `scripts/_migrations.py`) — then replaces any previous demo data. Safe to run
repeatedly.

Seeds two tenants:
  * "Demo Co"      — 7 days of traffic and a warm semantic cache
  * "Brand New Co" — no data at all, for checking the empty states

This writes only to the two demo tenants below, identified by the hash of their
fixed demo credentials; it never touches rows belonging to real tenants.
"""

import asyncio
import json
import math
import os
import pathlib
import random
import sys

import asyncpg

REPO = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from app.security import hash_token  # noqa: E402  (path setup must precede the import)
from app.services.embeddings import EMBEDDING_DIM  # noqa: E402  (same)
from scripts._migrations import (  # noqa: E402  (same)
    FIRST_MIGRATION,
    MIGRATIONS_DIR,
    apply_rerunnable_migrations,
)

DATABASE_URL = os.getenv(
    "DATABASE_URL", "postgresql://postgres:demopw@localhost:5433/semanticdb"
)

# Fixed, well-known, local-demo-only credentials — the whole point is that the
# README and the dashboard's .env.example can name them. Never seed these against
# anything reachable from outside your machine.
DEMO_PROXY_KEY = "sk-demo-tenant-key"
DEMO_DASHBOARD_TOKEN = "dash_demo-tenant-token"
EMPTY_PROXY_KEY = "sk-demo-empty-key"
EMPTY_DASHBOARD_TOKEN = "dash_demo-empty-token"

# Matches the column width the migrations create (EMBEDDING_DIM, default 1536).
EMBEDDING_DIMS = EMBEDDING_DIM

# (normalized_query, min_similarity, max_similarity). Hit counts are not listed
# here: they are derived from the generated traffic so the "top cached" table and
# the semantic-hit count in /v1/stats agree with each other. The final entry is
# never hit, which exercises the NULL min/max similarity path.
QUERIES = [
    ("what is your refund policy", 0.921, 0.999),
    ("how do i reset my password", 0.934, 0.991),
    ("what are your business hours", 0.945, 0.988),
    ("do you ship internationally", 0.928, 0.977),
    ("how do i cancel my subscription", 0.951, 0.996),
    ("what payment methods do you accept", 0.933, 0.962),
    ("where is my order", 0.940, 0.940),
    ("how long does delivery take", 0.922, 0.958),
    ("is there a free trial", 0.966, 0.981),
    ("how do i contact support", 0.930, 0.945),
    ("do you offer student discounts", 0.937, 0.984),
    ("what is the warranty period", 0.972, 0.972),
    ("can i change my shipping address", 0.925, 0.953),
    ("how do i update my billing details", 0.949, 0.968),
    ("do you have an api", 0.931, 0.974),
    ("what languages do you support", 0.958, 0.958),
    ("how do i export my data", 0.943, 0.961),
    ("is my data encrypted", 0.978, 0.978),
    ("do you offer phone support", 0.936, 0.936),
    ("how do i delete my account", None, None),
]


def unit_vector(seed: int) -> str:
    """Deterministic unit vector, as a pgvector literal."""
    rng = random.Random(seed)
    values = [rng.gauss(0, 1) for _ in range(EMBEDDING_DIMS)]
    norm = math.sqrt(sum(v * v for v in values)) or 1.0
    return "[" + ",".join(f"{v / norm:.6f}" for v in values) + "]"


def distribute_hits(total: int, buckets: int) -> list[int]:
    """Split `total` semantic hits across `buckets` queries, Zipf-style.

    Real cache traffic is heavily skewed — a handful of questions account for
    most of the hits — so a 1/rank weighting looks far more like production than
    an even split. Rounding drift is absorbed by the hottest bucket so the parts
    always sum to exactly `total`.
    """
    if buckets <= 0 or total <= 0:
        return [0] * max(buckets, 0)

    weights = [1.0 / (rank + 1) for rank in range(buckets)]
    scale = sum(weights)
    counts = [max(1, round(total * w / scale)) for w in weights]
    counts[0] += total - sum(counts)

    return counts


async def ensure_schema(conn: asyncpg.Connection) -> None:
    # 001 predates the IF NOT EXISTS convention, so only run it on a fresh
    # database. Everything after it is idempotent and safe to re-apply.
    if await conn.fetchval("SELECT to_regclass('public.tenants')") is None:
        await conn.execute((MIGRATIONS_DIR / FIRST_MIGRATION).read_text(encoding="utf-8"))
        print(f"  applied {FIRST_MIGRATION}")

    for name in await apply_rerunnable_migrations(conn):
        print(f"  applied {name}")

    # 001 still creates the plaintext api_key column on a fresh database. The demo
    # tenants below never populate it, and NOT NULL would block the insert. On a
    # database where scripts/migrate_hash_keys.py already dropped the column there
    # is nothing to relax, hence the guard.
    await conn.execute(
        """
        DO $$
        BEGIN
            IF EXISTS (
                SELECT 1 FROM information_schema.columns
                WHERE table_name = 'tenants' AND column_name = 'api_key'
            ) THEN
                ALTER TABLE tenants ALTER COLUMN api_key DROP NOT NULL;
            END IF;
        END $$;
        """
    )


async def upsert_tenant(
    conn: asyncpg.Connection, proxy_key: str, dashboard_token: str, name: str
):
    """Reuse the existing demo tenant row if there is one.

    Deleting and re-inserting would burn a new SERIAL id on every run, so the
    tenant id in the dashboard would change each time. Upserting keeps it stable.
    Only hashes are ever written — the plaintext demo credentials live in this
    file, not in the database.
    """
    return await conn.fetchrow(
        """
        INSERT INTO tenants (api_key_hash, dashboard_token_hash, name)
        VALUES ($1, $2, $3)
        -- Predicate must be restated: tenants_api_key_hash_idx is a partial index.
        ON CONFLICT (api_key_hash) WHERE api_key_hash IS NOT NULL DO UPDATE
            SET name = EXCLUDED.name,
                dashboard_token_hash = EXCLUDED.dashboard_token_hash
        RETURNING id, uuid
        """,
        hash_token(proxy_key),
        hash_token(dashboard_token),
        name,
    )


async def clear_demo_history(conn: asyncpg.Connection, tenant_id: int, tenant_uuid) -> None:
    entries = await conn.execute(
        "DELETE FROM cache_entries WHERE tenant_id = $1::uuid", str(tenant_uuid)
    )
    requests = await conn.execute("DELETE FROM requests WHERE tenant_id = $1", tenant_id)
    if entries != "DELETE 0" or requests != "DELETE 0":
        print(f"  cleared previous demo data ({entries.lower()}, {requests.lower()})")


async def seed_cache_entries(conn: asyncpg.Connection, tenant_uuid, semantic_hits: int) -> None:
    # The last query is deliberately left cold, so only the rest share the hits.
    hit_counts = distribute_hits(semantic_hits, len(QUERIES) - 1) + [0]

    for index, ((query, lo, hi), hits) in enumerate(zip(QUERIES, hit_counts)):
        await conn.execute(
            """
            INSERT INTO cache_entries (
                tenant_id, normalized_query, prompt_hash, response_json, embedding,
                model, hit_count, min_similarity, max_similarity, raw_query
            )
            VALUES ($1::uuid, $2, $3, $4::jsonb, $5::vector, $6, $7, $8, $9, $10)
            """,
            str(tenant_uuid),
            query,
            f"demo-hash-{index}",
            json.dumps(
                {
                    "choices": [
                        {"message": {"role": "assistant", "content": f"Answer to: {query}"}}
                    ]
                }
            ),
            unit_vector(index),
            "mistral-small-latest",
            hits,
            lo if hits else None,
            hi if hits else None,
            query[0].upper() + query[1:] + "?",
        )
    print(f"  seeded {len(QUERIES)} cache entries ({sum(hit_counts)} hits)")


async def seed_requests(conn: asyncpg.Connection, tenant_id: int) -> tuple[int, int]:
    """Insert 7 days of traffic. Returns (total_rows, semantic_hits)."""
    rng = random.Random(20260813)
    rows = []
    semantic_hits = 0

    # Seven days of traffic, oldest first, with the hit rate climbing as the
    # semantic cache warms up — which is the story the trend chart should tell.
    for days_ago in range(6, -1, -1):
        volume = rng.randint(420, 900)
        warmth = 0.28 + (6 - days_ago) * 0.075

        for _ in range(volume):
            roll = rng.random()
            if roll < warmth * 0.62:
                status, hit, latency = "EXACT_HIT", True, rng.randint(4, 22)
            elif roll < warmth:
                status, hit, latency = "SEMANTIC_HIT", True, rng.randint(18, 70)
                semantic_hits += 1
            elif roll < 0.96:
                status, hit, latency = "MISS", False, rng.randint(480, 1600)
            else:
                status, hit, latency = "UNCACHEABLE", False, rng.randint(350, 1300)

            rows.append(
                (
                    days_ago,
                    tenant_id,
                    f"demo-hash-{rng.randrange(len(QUERIES))}",
                    hit,
                    latency,
                    rng.randint(380, 1500),   # prompt_tokens
                    rng.randint(180, 900),    # completion_tokens
                    status,
                )
            )

    await conn.executemany(
        """
        INSERT INTO requests (timestamp, tenant_id, prompt_hash, hit, latency_ms, model,
                              prompt_tokens, completion_tokens, cache_status)
        VALUES (now() - ($1::int * INTERVAL '1 day') - (random() * INTERVAL '18 hours'),
                $2, $3, $4, $5, 'mistral-small-latest', $6, $7, $8)
        """,
        rows,
    )
    return len(rows), semantic_hits


async def main() -> int:
    print(f"Connecting to {DATABASE_URL.rsplit('@', 1)[-1]}")
    try:
        conn = await asyncpg.connect(DATABASE_URL)
    except Exception as exc:
        print(f"\nCould not connect: {type(exc).__name__}: {exc}")
        print("Is the demo Postgres running? See scripts/README.md")
        return 1

    try:
        await ensure_schema(conn)

        demo = await upsert_tenant(conn, DEMO_PROXY_KEY, DEMO_DASHBOARD_TOKEN, "Demo Co")
        empty = await upsert_tenant(conn, EMPTY_PROXY_KEY, EMPTY_DASHBOARD_TOKEN, "Brand New Co")

        await clear_demo_history(conn, demo["id"], demo["uuid"])
        await clear_demo_history(conn, empty["id"], empty["uuid"])

        # Requests first: the cache entries' hit counts are derived from how many
        # semantic hits the generated traffic actually contains.
        total, semantic_hits = await seed_requests(conn, demo["id"])
        print(f"  seeded {total} requests across 7 days ({semantic_hits} semantic hits)")
        await seed_cache_entries(conn, demo["uuid"], semantic_hits)

        print("\nDone.")
        print(f"  Demo Co       -> tenant_id = {demo['id']}   (has data)")
        print(f"    proxy key       {DEMO_PROXY_KEY}")
        print(f"    dashboard token {DEMO_DASHBOARD_TOKEN}")
        print(f"  Brand New Co  -> tenant_id = {empty['id']}   (empty, for empty states)")
        print(f"    proxy key       {EMPTY_PROXY_KEY}")
        print(f"    dashboard token {EMPTY_DASHBOARD_TOKEN}")
        print(
            "\nThe dashboard picks its tenant from GATEWAY_DASHBOARD_TOKEN, not from a"
            "\ntenant id - set it in dashboard/.env.local to switch which tenant you see."
        )
    finally:
        await conn.close()

    return 0


sys.exit(asyncio.run(main()))
