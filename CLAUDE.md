# Semantic Gateway

A caching reverse proxy in front of an LLM API. Each request to
`/v1/chat/completions` checks a Redis exact-match cache, then a pgvector semantic
cache (with match verification against polarity flips and entity/number swaps),
and only calls the upstream model on a miss in both. Every request is logged to
Postgres; `/v1/stats*` and the Next.js dashboard report hit rate and savings.
README.md is the full reference; this file is the working summary.

## Stack

- **API:** FastAPI (`app/`), run with uvicorn. Python 3.11 in Docker.
- **Postgres 16 + pgvector** (`pgvector/pgvector:pg16`): tenants, request log,
  `cache_entries` with an HNSW cosine index on `embedding`.
- **Redis:** exact-match cache and the per-tenant rate limiter.
- **Upstream:** Mistral chat completions (`mistral-small-latest`).
- **Embeddings:** OpenAI `text-embedding-3-small`, 1536 dimensions.
- **Dashboard:** Next.js 14 (`dashboard/`), proxies to the gateway server-side.
- **Deploy:** Railway (`railway.json`, `Dockerfile`); dashboard on Vercel.

## Layout

- `app/routes/proxy.py`: the request pipeline (auth, rate limit, exact cache,
  embed, semantic match, upstream, store, log).
- `app/routes/stats.py`: read-only stats endpoints (dashboard token only).
- `app/services/`: `cache` (Redis), `semantic_cache` (pgvector),
  `match_verification`, `cacheability`, `normalization`, `embeddings`, `rate_limit`.
- `app/auth.py`, `app/security.py`: scoped bearer tokens. `sk-...` for the proxy,
  `dash_...` for stats; only SHA-256 hashes are stored.
- `app/db.py`: pool, startup checks (`verify_auth_schema`,
  `verify_embedding_dimension`), request logging.
- `migrations/001`-`006`: applied in filename order. 002 onward are idempotent;
  001 runs only on a fresh database.
- `scripts/_migrations.py`: globs and applies migrations (renders the vector width).
- `scripts/migrate_hash_keys.py`: hashes keys, issues dashboard tokens, drops the
  plaintext column. `--mode=in-place|rotate` is required.
- `scripts/seed_demo.py`: demo tenants and history for the dashboard (port 5433).

## Running

```bash
docker-compose up --build            # app :8000, redis :6379, postgres :5432
python -m pytest -q                  # unit tests; no network, no database
npm --prefix dashboard run dev       # dashboard :3000
```

## Branches

- `main`: phases 1-3 merged (gateway, semantic cache + verification, stats and
  dashboard).
- `phase-3.5-security-auth-hardening`: scoped hashed tokens, rate limiting,
  stats CORS/access log, migrations 005-006. Pushed, not yet merged.
- `local-model-testing`: branched from 3.5; adds the Ollama local-testing setup.
  Local only.

## Project rules

- **Test requests use `temperature=0`.** Streaming, tool calls, non-zero
  temperature, and personalised or time-sensitive prompts ("my order", "today",
  "latest") bypass the cache entirely and come back `X-Cache: UNCACHEABLE`.
- **Migrations run as the database master user** (`postgres` locally). 002 creates
  the `vector` extension, which needs it.
- **`migrate_hash_keys.py --mode=rotate` mints new proxy keys on every run** and
  breaks every existing key. Never run it in a retry loop or unattended, and never
  against anything but a local test database without an explicit decision.
- **Production defaults do not change:** OpenAI embeddings at 1536 dimensions,
  Mistral completions, similarity threshold 0.92. Every local-testing difference
  is an environment variable; with none set, the code resolves to production
  (pinned by `tests/test_provider_defaults.py`).
- Never point local tooling at the Railway database or any remote service.

## Local testing with Ollama

Runs the whole gateway on this machine: Ollama for embeddings and completions,
Postgres and Redis in Docker. Nothing remote is contacted.

**Models:** `nomic-embed-text` for embeddings (768 dims, verified), `phi3:latest`
for completions (the smallest installed chat model). `gemma4:e2b` is installed but
cannot embed (Ollama returns 501). `phi3` *can* embed but scores the wrong way round
(rewordings below opposites) at 3072 dims, past HNSW's 2000 limit, so don't use it.

```bash
ollama pull nomic-embed-text                 # once
cp .env.local.example .env.local             # once
python scripts/local_setup.py                # stack up, fresh DB, migrations, credentials
python scripts/run_local_gateway.py          # gateway on :8010 (or launch config "gateway-local")
python scripts/local_smoke_test.py           # end-to-end checks, one line per case
docker compose -f docker-compose.local.yml down -v   # tear down and wipe
```

How it fits together:

- **Config is env-only.** `EMBEDDING_BASE_URL`, `EMBEDDING_API_KEY` (falls back to
  `OPENAI_API_KEY`), `EMBEDDING_MODEL`, `EMBEDDING_DIM`, `UPSTREAM_BASE_URL`,
  `UPSTREAM_MODEL`, `MISTRAL_API_KEY` (a dummy for Ollama),
  `SEMANTIC_CACHE_THRESHOLD`. `EMBEDDING_PROVIDER=openai` means any
  OpenAI-compatible endpoint, which Ollama's `/v1` is.
- **Vector width.** `002_semantic_cache.sql` still says `vector(1536)`;
  `scripts/_migrations.py` rewrites that literal only when `EMBEDDING_DIM` differs,
  so production SQL is unchanged. The app refuses to start if
  `cache_entries.embedding` does not match `EMBEDDING_DIM`. Changing the dimension
  needs a fresh database (`local_setup.py` always makes one).
- **Only `.env.local` is read.** The app's `load_dotenv()` walks up from `app/` and,
  from a worktree, finds the main checkout's `.env`. The local scripts set
  `PYTHON_DOTENV_DISABLED=1` and refuse any non-localhost URL. Always start the
  local gateway through `run_local_gateway.py`, never plain uvicorn.
- **Ports:** Postgres 55432, Redis 56379, gateway 8010 (8011 for the smoke test's
  broken-embedding gateway). If the gateway itself runs in Docker, use
  `http://host.docker.internal:11434/v1` for both base URLs.
- **Threshold 0.80 is for local testing only.** nomic scores rewordings around
  0.97, unrelated prompts around 0.3-0.4, and the near-miss pairs around 0.81-0.93.
  0.80 sends the near-misses through match verification, which is the point.
- **Credentials** from `local_setup.py` land in `.local-credentials.json`
  (gitignored) and are never printed. Every setup run issues new ones.
- **phi3 misses take about 10-15s** each (`max_tokens=60`); a full smoke run is a
  few minutes.

Known behaviour the smoke test records rather than fixes:

- An embedding failure (wrong model name, Ollama down) is an unhandled 500 from
  `embed_text()` in `proxy.py`, with no fallback to upstream.
- Entity verification treats two queries as different only when they share *no*
  capitalised word or number, so "Delhi in July" vs "Mumbai in July" and "v2 of the
  API" vs "v3 of the API" are served from each other's cache.
