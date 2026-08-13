# Semantic Gateway

A caching reverse proxy in front of an LLM API (Mistral). It cuts redundant upstream
calls by checking two caches before ever calling the model:

1. **Exact-match cache (Redis)** — same tenant, same normalized request, byte-for-byte.
2. **Semantic cache (Postgres + pgvector)** — same tenant, a *rephrased* version of a
   question it has already answered, matched via embedding cosine similarity and then
   verified (`app/services/match_verification.py`) so that near-identical embeddings
   carrying opposite or different meaning are not served from cache.

`cache_entries` stores both the `normalized_query` (what gets embedded and matched) and
the `raw_query` (added in `migrations/003_raw_query.sql`). Verification runs against the
raw text because normalization lowercases everything, which would erase the
capitalization the entity check depends on. Rows written before that migration have a
`NULL` `raw_query` and fall back to the normalized form, where the polarity, negation,
and number checks still apply.

Only on a miss in both does it call Mistral, and it stores the result in both caches
for next time.

## Architecture

```
client -> gateway (FastAPI)
            |
            +--> normalize query, check cacheability
            |
            +--> Redis exact-match check          -> hit: X-Cache: EXACT_HIT
            |
            +--> embed query, pgvector similarity
            |       search (Postgres), top 5 by distance
            |          |
            |          +--> for each candidate over the threshold, verify the
            |          |      match against the raw (un-normalized) query text:
            |          |      reject on polarity/negation flips ("enable" vs
            |          |      "disable") or swapped entities and numbers
            |          |      ("Delhi" vs "Mumbai", "version 2" vs "3")
            |          |
            |          +--> first candidate that passes -> X-Cache: SEMANTIC_HIT
            |                 all rejected -> falls through to a MISS
            |
            +--> Mistral (on miss)                 -> X-Cache: MISS
            |       |
            |       +--> store in Redis + pgvector for next time
            |
            +--> uncacheable requests (streaming, tools,
                    non-zero temperature, personalized/fresh
                    queries) skip both caches entirely -> X-Cache: UNCACHEABLE

Every request is logged to Postgres (tenant, latency, tokens, cache_status, similarity).
```

Tenants authenticate with a bearer API key (`app/auth.py`), which resolves to both an
integer `tenant_id` (used for Postgres request logging) and a `tenant_uuid` (used for
the semantic cache tables, which key on `tenants.uuid`).

## Setup

1. Copy `.env.example` to `.env` and fill in real values:

   ```bash
   cp .env.example .env
   ```

   At minimum you need `MISTRAL_API_KEY` (upstream LLM), `OPENAI_API_KEY` (embeddings),
   and a `POSTGRES_PASSWORD` (used both by the `postgres` container and inside
   `DATABASE_URL`).

2. Start the stack:

   ```bash
   docker-compose up --build
   ```

   This runs the FastAPI app on `localhost:8000`, Redis, and a Postgres instance with
   the `pgvector` extension available (`pgvector/pgvector:pg16` image).

3. Run the migrations against the running Postgres container:

   ```bash
   docker exec -i llm-cache-gateway-postgres psql -U postgres -d semanticdb < migrations/001_migrations.sql
   docker exec -i llm-cache-gateway-postgres psql -U postgres -d semanticdb < migrations/002_semantic_cache.sql
   docker exec -i llm-cache-gateway-postgres psql -U postgres -d semanticdb < migrations/003_raw_query.sql
   ```

4. Insert a tenant row so you have an API key to call the gateway with:

   ```sql
   INSERT INTO tenants (api_key, name) VALUES ('demo-key', 'Demo Tenant');
   ```

### Required environment variables

See [`.env.example`](.env.example) for the full list. The important ones:

| Variable | Purpose |
|---|---|
| `MISTRAL_API_KEY` | Upstream LLM used on a full cache miss |
| `OPENAI_API_KEY` | Embeddings provider for the semantic cache |
| `DATABASE_URL` | Postgres connection string |
| `REDIS_URL` | Redis connection string |
| `SEMANTIC_CACHE_THRESHOLD` | Cosine-similarity cutoff for a semantic hit (default `0.92`) |
| `CACHE_TTL_SECONDS` | Redis exact-match cache TTL (default `86400`) |

## Trying the demo

First call — a full miss, answered by Mistral and cached in both layers:

```bash
curl -s http://localhost:8000/v1/chat/completions \
  -H "Authorization: Bearer demo-key" \
  -H "Content-Type: application/json" \
  -d '{"messages": [{"role": "user", "content": "What is pgvector?"}], "temperature": 0}' \
  -D - -o /dev/null | grep -i x-cache
# X-Cache: MISS
```

Now ask a reworded version of the same question — it should come back as a
`SEMANTIC_HIT` instead of hitting Mistral again:

```bash
curl -s http://localhost:8000/v1/chat/completions \
  -H "Authorization: Bearer demo-key" \
  -H "Content-Type: application/json" \
  -d '{"messages": [{"role": "user", "content": "Can you explain what pgvector is?"}], "temperature": 0}' \
  -D - -o /dev/null | grep -i x-cache
# X-Cache: SEMANTIC_HIT
```

## Seeding demo data

To look at the stats endpoints (or the dashboard) without driving real traffic first,
`scripts/seed_demo.py` writes a tenant with seven days of history and a warm semantic
cache. It applies migrations `001`–`004` if needed and is safe to re-run — it replaces
its own data and keeps the same tenant ids.

```bash
python scripts/seed_demo.py
```

It reads `DATABASE_URL` from the environment, defaulting to
`postgresql://postgres:demopw@localhost:5433/semanticdb`. If port 5432 is already taken
by a local Postgres install, a throwaway pgvector instance on 5433 keeps them apart:

```bash
docker run -d --name semgw-demo -e POSTGRES_PASSWORD=demopw -e POSTGRES_DB=semanticdb -p 5433:5432 pgvector/pgvector:pg16
```

It seeds two tenants: `tenant_id=1` with data, and `tenant_id=2` with none (for checking
empty states). It only ever touches tenants whose `api_key` starts with `demo-`.

## Stats endpoints

All three take a `tenant_id` query parameter and back the dashboard in `dashboard/`.

`GET /v1/stats?tenant_id=<id>` aggregates the `requests` log table: total requests,
counts and average latency per `cache_status`, overall hit rate, and the tokens never
sent upstream. Prompt and completion tokens are reported **separately**
(`prompt_tokens_saved` / `completion_tokens_saved`) since every provider prices them
differently; `estimated_cost_saved_usd` keeps a single blended figure using
`COST_PER_1K_TOKENS`.

`GET /v1/stats/trend?tenant_id=<id>` groups the last 7 days by day, oldest first, as
`{date, total_requests, hit_count, hit_rate}`. Days with no traffic are omitted rather
than reported as zero.

`GET /v1/stats/top-cached?tenant_id=<id>` returns the 20 most-hit `cache_entries` with
the similarity range each has served. `min_similarity`/`max_similarity` are `NULL` until
an entry has been served at least once. Note this counts *semantic* hits only — exact
hits are answered from Redis and never touch `cache_entries`.

```bash
curl -s "http://localhost:8000/v1/stats?tenant_id=1"
```

## Tests

```bash
pip install -r requirements.txt pytest pytest-asyncio
pytest tests/
```
