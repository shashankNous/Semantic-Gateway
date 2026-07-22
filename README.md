# Semantic Gateway

A caching reverse proxy in front of an LLM API (Mistral). It cuts redundant upstream
calls by checking two caches before ever calling the model:

1. **Exact-match cache (Redis)** — same tenant, same normalized request, byte-for-byte.
2. **Semantic cache (Postgres + pgvector)** — same tenant, a *rephrased* version of a
   question it has already answered, matched via embedding cosine similarity.

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
            +--> embed query, pgvector similarity  -> hit: X-Cache: SEMANTIC_HIT
            |       search (Postgres)
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

## Stats endpoint

`GET /v1/stats?tenant_id=<id>` aggregates the `requests` log table for a tenant:
total requests, counts and average latency per `cache_status`, overall hit rate, and
an estimated token/cost savings figure (sum of prompt + completion tokens on cache
hits — tokens that were never sent to Mistral).

```bash
curl -s "http://localhost:8000/v1/stats?tenant_id=1"
```

## Tests

```bash
pip install -r requirements.txt pytest pytest-asyncio
pytest tests/
```
