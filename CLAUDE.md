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
- **Embeddings:** OpenAI `text-embedding-3-small`, 1536 dimensions, by default;
  a `mistral` preset (`mistral-embed`, 1024, padded to 1536) is available.
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
python -m pytest -q                  # tests/ only (pytest.ini); no network, no database
npm --prefix dashboard run dev       # dashboard :3000
```

## Branches

- `main`: phases 1-3 merged (gateway, semantic cache + verification, stats and
  dashboard).
- `phase-3.5-security-auth-hardening`: scoped hashed tokens, rate limiting,
  stats CORS/access log, migrations 005-006. Pushed, not yet merged.
- `local-model-testing`: branched from 3.5; adds the Ollama local-testing setup.
  Local only.
- `fix-verification-and-embedding-failures`: branched from `local-model-testing`;
  exact entity matching, embedding-failure degradation, single-path `.env`
  loading, per-request embedding size check. Local only.
- `reconcile-wip-with-fixes`: branched from the fixes branch; brings over the
  `wip-main-folder-changes` snapshot (Mistral embeddings, stats `models`, dashboard
  pricing, rehearsal scripts) with the Mistral work ported onto the fixes design.
  Local only.

## Project rules

- **NEVER MIX EMBEDDING MODELS WITHIN ONE TENANT'S `cache_entries`.** Vectors from
  different models are not comparable even when they fit the same column: a
  padded `mistral-embed` vector and a native OpenAI one still produce a cosine
  score, and it means nothing. No check catches it, because every row looks valid
  on its own. Changing `EMBEDDING_PROVIDER` or `EMBEDDING_MODEL` means a fresh
  tenant, or clearing that tenant's `cache_entries` first.
- **Test requests use `temperature=0`.** Streaming, tool calls, non-zero
  temperature, and personalised or time-sensitive prompts ("my order", "today",
  "latest") bypass the cache entirely and come back `X-Cache: UNCACHEABLE`.
- **Migrations run as the database master user** (`postgres` locally). 002 creates
  the `vector` extension, which needs it.
- **`migrate_hash_keys.py --mode=rotate` mints new proxy keys on every run** and
  breaks every existing key. Never run it in a retry loop or unattended, and never
  against anything but a local test database without an explicit decision.
- **Production defaults do not change:** OpenAI embeddings at 1536 dimensions,
  Mistral completions, similarity threshold 0.92. (The one deliberate exception:
  the embedding timeout went from 30s to a configurable 10s.) Every local-testing difference
  is an environment variable; with none set, the code resolves to production
  (pinned by `tests/test_provider_defaults.py`).
- Never point local tooling at the Railway database or any remote service.

## Behaviour worth knowing

- **Entity matching is exact.** `has_entity_mismatch()` extracts numbers, version
  tokens (`v2` = `V2` = the `2` in "version 2") and capitalised words that don't
  open a sentence (minus `I` and a stopword list of question/function words; not
  `may`/`will`, which capitalised are a month or a name). An **all-caps token of
  two or more letters is always an entity** (US, IT, WHO), even sentence-initial
  or on the stopword list, unless the whole question is upper case
  ("WHAT IS PGVECTOR" still matches "what is pgvector"). Two
  queries that both name entities must name the *same set*: "Delhi in July" vs
  "Mumbai in July" is refused, and so is "Docker" vs "Docker on Ubuntu". A query
  naming no entities is not compared. Trade-off: correctness over hit rate, so a
  valid paraphrase that adds or drops a proper noun is a miss.
- **Embedding failures degrade, they don't fail.** Any failure in `embed_text()`
  (unreachable, timeout, HTTP error, malformed response, wrong vector length,
  missing key) raises `EmbeddingUnavailableError`. The proxy logs one warning,
  skips the semantic cache, calls upstream, writes the Redis exact cache but no
  `cache_entries` row, logs `MISS`, and adds `X-Cache-Degraded:
  embedding-unavailable`. Exact hits are unaffected.
- **Embedding timeout: `EMBEDDING_TIMEOUT_SECONDS`, default 10.** This *is* a
  changed production default (it was a hardcoded 30s): a hung embedding service
  delays every exact-cache miss by the full timeout before degrading, so it is kept
  short. Must be a finite number greater than 0, or the app refuses to start.
  `.env.local` uses 60, because the first Ollama call loads the model.
- **Embedding providers are presets.** `EMBEDDING_PROVIDER` (optional; unset
  means `openai`) fills in defaults only: `openai` is api.openai.com,
  `OPENAI_API_KEY`, `text-embedding-3-small`, native 1536; `mistral` is
  api.mistral.ai, `MISTRAL_API_KEY`, `mistral-embed`, native 1024.
  `EMBEDDING_BASE_URL`, `EMBEDDING_API_KEY`, `EMBEDDING_MODEL` and
  `EMBEDDING_NATIVE_DIM` override them one by one. Any other provider name stops
  the app at startup, listing the valid ones.
- **Two widths.** `EMBEDDING_DIM` is the `cache_entries.embedding` column (default
  1536; migrations and the startup database check use it). `EMBEDDING_NATIVE_DIM`
  is what the model returns: explicit, else the preset's, else `EMBEDDING_DIM`.
  It must be between 1 and `EMBEDDING_DIM`, or the app refuses to start. The old
  wip name `EMBEDDING_DIMS` is refused at startup with the rename.
- **Padding.** Every response is checked against the native width (mismatch:
  `EmbeddingUnavailableError`, so a degraded miss), then right-padded with zeros to
  the column. Appending the same zeros to both vectors leaves cosine similarity
  exactly unchanged, so padded scores are the model's true scores; the local
  padding run reproduces case 3's 0.968510 to the last printed digit.
- **`.env` comes from one path.** `app/env.py:load_env()` reads `ENV_FILE` if set,
  else `<project root>/.env` (root from the file's own location), never searches
  parent folders, and never overrides real environment variables. Don't call
  `load_dotenv()` directly in `app/`; a test enforces this.

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

- **Config is env-only.** `EMBEDDING_BASE_URL`, `EMBEDDING_API_KEY`,
  `EMBEDDING_MODEL`, `EMBEDDING_DIM`, `UPSTREAM_BASE_URL`, `UPSTREAM_MODEL`,
  `MISTRAL_API_KEY` (a dummy for Ollama), `SEMANTIC_CACHE_THRESHOLD`.
  `EMBEDDING_PROVIDER` stays **unset** locally: a preset would also set the native
  width (1536 or 1024), and nomic returns 768.
- **Vector width.** `002_semantic_cache.sql` still says `vector(1536)`;
  `scripts/_migrations.py` rewrites that literal only when `EMBEDDING_DIM` differs,
  so production SQL is unchanged. The app refuses to start if
  `cache_entries.embedding` does not match `EMBEDDING_DIM`. Changing the dimension
  needs a fresh database (`local_setup.py` always makes one).
- **Padding run.** `.env.local.padded` (gitignored; copy `.env.local` and swap in
  the commented `EMBEDDING_DIM=1536` / `EMBEDDING_NATIVE_DIM=768` alternative) keeps
  a production-width `vector(1536)` column. Select it with `LOCAL_ENV_FILE`, which
  every script and subprocess inherits:
  `LOCAL_ENV_FILE=.env.local.padded python scripts/local_setup.py`, the
  `gateway-local-padded` launch config (or `run_local_gateway.py --env-file
  .env.local.padded`), then the smoke test with the same `LOCAL_ENV_FILE`. Case 3b
  compares case 3's score with the last run at the other width
  (`.local-smoke-similarity.json`, gitignored).
- **Only the local env file is read.** The local scripts set `PYTHON_DOTENV_DISABLED=1`,
  which also switches off `app/env.py`, and refuse any non-localhost URL. Always
  start the local gateway through `run_local_gateway.py`, never plain uvicorn.
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

What the smoke test checks beyond the cache basics: verification refusing
near-miss and shared-entity pairs (with the verifier's reason), the rate limiter
failing closed with Redis down, and a broken embedding model degrading to a miss.
