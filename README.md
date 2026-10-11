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

## Authentication

Every tenant holds **two** bearer credentials, and they are not interchangeable:

| Credential | Prefix | Scope | Stored as |
|---|---|---|---|
| Proxy key | `sk-…` | `POST /v1/chat/completions` | `tenants.api_key_hash` |
| Dashboard token | `dash_…` | `GET /v1/stats*` (read-only) | `tenants.dashboard_token_hash` |

The split exists because the dashboard token is the one that ends up in a deployed
environment: a leaked `dash_…` exposes usage statistics, while a leaked `sk-…` runs up
an LLM bill. Presenting either one to the other's routes returns `401` — a valid
credential in the wrong scope is treated exactly like an unknown one.

**Only SHA-256 hashes are stored.** `app/db.py` hashes the presented token and matches
on the hash; the plaintext is never written to Postgres and never compared as a string,
so a database dump yields no usable credential. Plain SHA-256 rather than a slow KDF is
deliberate — these are 192-bit random tokens, not passwords, so there is no dictionary
to slow an attacker down against (see `app/security.py`).

Authentication resolves a tenant to both an integer `tenant_id` (used for Postgres
request logging) and a `tenant_uuid` (used for the semantic cache tables, which key on
`tenants.uuid`). **No route takes a tenant identifier from the caller** — the tenant is
always derived from the token, so there is no cross-tenant read to attempt.

Issue credentials with:

```bash
python scripts/migrate_hash_keys.py --mode=in-place
```

That backfills hashes for existing tenants, mints a `dash_…` token for any tenant
lacking one, and prints each new token **once**. A hash cannot be reversed, so a token
not captured from that output has to be reissued.

### Rate limiting

`POST /v1/chat/completions` is throttled per tenant using the same Redis instance as
the exact-match cache: `INCR tenant:{id}:count` with an `EXPIRE` of
`RATE_LIMIT_WINDOW_SECONDS` (default `60`), rejecting with `429` and a `Retry-After`
header past the threshold. The limit is `RATE_LIMIT_PER_MINUTE` (default `100`), so
both can be tuned per partner without a redeploy; `0` disables throttling entirely.
Throttling runs *before* the cache lookups and the upstream call, so a rejected
request costs nothing.

`RATE_LIMIT_PER_MINUTE` keeps that name for compatibility with deployments already
setting it, but it is really *requests per window* — only literally per-minute at the
default window. `RATE_LIMIT_WINDOW_SECONDS` is also the `Retry-After` value on both
the `429` and the fail-closed `503`, so a caller that honours it retries into a fresh
window rather than straight back into the same one.

#### When Redis is unreachable

There is no safe answer here, only a choice of which failure you would rather have.
`RATE_LIMIT_FAIL_MODE` makes it explicit:

| Mode | Behaviour | You get | You lose |
|---|---|---|---|
| `closed` *(default)* | Reject with `503` | The cap holds; no tenant can exceed its limit during the outage | Availability — the proxy is down for everyone while Redis is |
| `open` | Serve the request uncapped | Availability — a cache outage does not become a gateway outage | The cap; every tenant is unthrottled for the duration |

The default is `closed` because an unthrottled window is the more expensive failure.
Cache misses go upstream and cost real money per request, with no ceiling — an outage
that removes the cap is exactly when abuse is cheapest to attempt. A `503` is loud,
bounded, and clears itself the moment Redis returns; an unnoticed spend does neither.

Choose `open` if availability matters more than the cap and upstream spend is bounded
some other way (a hard budget on the Mistral account, a WAF in front, a trusted-partner
allow-list). Note this is a deliberate change from the earlier behaviour, which always
failed open.

A value other than `open` or `closed` is a startup error rather than a silent fallback
— a typo like `close` must not quietly pick a posture nobody asked for.

The `503` carries `Retry-After`.

For `open` to mean anything, the layer after the limiter has to tolerate Redis being
gone too — otherwise the request passes the limiter and then dies in the cache lookup.
So the exact-match cache degrades rather than raises: a read during an outage becomes a
miss, a write becomes a no-op, and the request is answered upstream
(`app/services/cache.py`).

That is the opposite default from the limiter, deliberately. A cache miss costs one
upstream call and corrects itself; a missing rate limit removes the ceiling on how many
upstream calls one tenant can trigger.

### Stats access log

Every call to `/v1/stats*` — including rejected ones — is recorded in `stats_access_log`
(`tenant_id`, `endpoint`, `timestamp`, `requester_ip`, `status_code`) by a middleware in
`app/middleware.py`, so there is no per-route boilerplate. `tenant_id` is `NULL` on a
`401`, which is exactly the access worth keeping a record of. Writes are best-effort and
never fail the request.

```sql
SELECT tenant_id, endpoint, count(*), max(timestamp)
FROM stats_access_log
WHERE timestamp > now() - INTERVAL '1 day'
GROUP BY tenant_id, endpoint
ORDER BY count DESC;
```

### CORS

`/v1/stats*` sends `Access-Control-Allow-Origin` only for origins listed in
`DASHBOARD_ORIGIN` (comma-separated, so staging and production can differ). Unset means
no cross-origin browser access at all, which is the correct default: the dashboard
proxies through its own Next server and sends no `Origin` header, so locking this down
breaks nothing.

The proxy route is deliberately left alone. It has **no** CORS headers today, which
means browsers already cannot call it cross-origin — it is closed, not wide open, so
there was nothing to tighten. Server-to-server callers are unaffected either way.

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

   On a fresh database the app container will fail its startup check until steps 3
   and 4 below are done — see [Deploy order](#deploy-order). Bring it back with
   `docker-compose restart` once they are.

3. Run the migrations against the running Postgres container:

   ```bash
   for f in migrations/*.sql; do docker exec -i llm-cache-gateway-postgres psql -U postgres -d semanticdb < "$f"; done
   ```

4. Create a tenant and issue its two credentials. `migrate_hash_keys.py` prints them
   once — copy them now:

   ```bash
   python scripts/migrate_hash_keys.py --mode=in-place
   ```

   For an empty database, `python scripts/seed_demo.py` creates demo tenants with
   fixed local-only credentials instead.

   Steps 3 and 4 are ordered relative to the app, not just to each other. See
   [Deploy order](#deploy-order).

### Migrations

| File | What it adds |
|---|---|
| `001_migrations.sql` | `tenants`, `requests` |
| `002_semantic_cache.sql` | pgvector, `cache_entries`, `tenants.uuid` |
| `003_raw_query.sql` | `cache_entries.raw_query` |
| `004_cache_entry_stats.sql` | `cache_entries.min_similarity` / `max_similarity` |
| `005_token_scopes.sql` | `tenants.api_key_hash`, `tenants.dashboard_token_hash` |
| `006_stats_access_log.sql` | `stats_access_log` |

`002`–`006` are idempotent (`ADD COLUMN IF NOT EXISTS` / `CREATE … IF NOT EXISTS`) and
safe to re-run. `001` predates that convention, so only apply it to a fresh database.

Both operator scripts apply the re-runnable set for you, and they do it by **globbing
`migrations/` rather than naming files** — an idempotent `007_*.sql` dropped into the
directory is picked up with no change to either script (`scripts/_migrations.py`).
`scripts/seed_demo.py` additionally applies `001` when it finds a fresh database;
`scripts/migrate_hash_keys.py` refuses to run without it.

`migrate_hash_keys.py` also backfills credentials and drops the plaintext
`tenants.api_key` column. Run it rather than applying `005` by hand — applying `005`
alone leaves the hash columns `NULL`, and authentication fails closed until the
backfill runs. See [Deploy order](#deploy-order).

### Deploy order

**Migrations → backfill → app.** The order is not optional. `app/db.py` authenticates
against `tenants.api_key_hash` / `tenants.dashboard_token_hash` with **no plaintext
fallback**, so an app serving traffic against a database whose backfill has not
finished returns `401` to *every* request. At the caller that is indistinguishable
from a bad key; in the logs it is just a stream of ordinary 401s.

1. **Apply migrations `001`–`006`**, in order (`001` only on a fresh database).

   ```bash
   for f in migrations/*.sql; do psql "$DATABASE_URL" -f "$f"; done
   ```

   On RDS this must run as the **master user**, which carries `rds_superuser`:
   `002_semantic_cache.sql` issues `CREATE EXTENSION vector`, and pgvector ships
   untrusted (`trusted = false`), so no lower-privileged application role can install
   it. Step 2 uses the same `DATABASE_URL` and inherits the requirement.

2. **Run the backfill to completion.** This populates the hash columns and mints a
   `dash_…` token per tenant. It prints new credentials **once** — capture them.

   ```bash
   python scripts/migrate_hash_keys.py --mode=in-place --dry-run   # inspect first
   python scripts/migrate_hash_keys.py --mode=in-place
   ```

3. **Only then deploy or restart the app.**

Steps 1 and 2 must finish before the app *process starts*, not merely before the first
real request. On an existing deployment that means running them against the live
database and then restarting — not the other way around.

The app enforces this rather than trusting the checklist. `verify_auth_schema()` runs
at startup (`app/main.py` → `app/db.py`) and **refuses to start** if the hash columns
are missing, or if any tenant still has a `NULL` `api_key_hash`, with an error naming
the step that was skipped. A tenant missing only its `dashboard_token_hash` logs a
warning and starts — that breaks `/v1/stats*` for one tenant, which does not justify
taking the whole gateway down. An empty `tenants` table is not an error.

One consequence worth expecting: on a first-time `docker-compose up`, the app
container fails to start until steps 1 and 2 are done. That is the check working.

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
| `RATE_LIMIT_PER_MINUTE` | Per-tenant proxy throttle, in requests per window (default `100`, `0` disables) |
| `RATE_LIMIT_WINDOW_SECONDS` | Length of that window, and the `Retry-After` value (default `60`) |
| `RATE_LIMIT_FAIL_MODE` | `closed` (default, `503`) or `open` (pass through) when Redis is down |
| `DASHBOARD_ORIGIN` | Comma-separated browser origins allowed to read `/v1/stats*` (default: none) |

## Trying the demo

First call — a full miss, answered by Mistral and cached in both layers:

```bash
curl -s http://localhost:8000/v1/chat/completions \
  -H "Authorization: Bearer sk-demo-tenant-key" \
  -H "Content-Type: application/json" \
  -d '{"messages": [{"role": "user", "content": "What is pgvector?"}], "temperature": 0}' \
  -D - -o /dev/null | grep -i x-cache
# X-Cache: MISS
```

Now ask a reworded version of the same question — it should come back as a
`SEMANTIC_HIT` instead of hitting Mistral again:

```bash
curl -s http://localhost:8000/v1/chat/completions \
  -H "Authorization: Bearer sk-demo-tenant-key" \
  -H "Content-Type: application/json" \
  -d '{"messages": [{"role": "user", "content": "Can you explain what pgvector is?"}], "temperature": 0}' \
  -D - -o /dev/null | grep -i x-cache
# X-Cache: SEMANTIC_HIT
```

## Seeding demo data

To look at the stats endpoints (or the dashboard) without driving real traffic first,
`scripts/seed_demo.py` writes a tenant with seven days of history and a warm semantic
cache. It applies every migration in `migrations/` if needed — globbed, not named, so a
later `007_*.sql` needs no change to the script — and is safe to re-run, replacing its
own data and keeping the same tenant ids.

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
empty states), and prints the credentials for each:

| Tenant | Proxy key | Dashboard token |
|---|---|---|
| Demo Co (has data) | `sk-demo-tenant-key` | `dash_demo-tenant-token` |
| Brand New Co (empty) | `sk-demo-empty-key` | `dash_demo-empty-token` |

These are fixed, well-known, local-demo-only credentials — the whole point is that
this README can name them. **Never seed them against a database reachable from
outside your machine.** The script only ever touches these two tenants, matched by
the hash of the keys above; it never touches real tenant rows.

## Stats endpoints

All three require a `dash_…` bearer token and back the dashboard in `dashboard/`. There
is **no** `tenant_id` parameter — the tenant is derived from the token, so a caller can
only ever read its own numbers.

`GET /v1/stats` aggregates the `requests` log table: total requests,
counts and average latency per `cache_status`, overall hit rate, and the tokens never
sent upstream. Prompt and completion tokens are reported **separately**
(`prompt_tokens_saved` / `completion_tokens_saved`) since every provider prices them
differently; `estimated_cost_saved_usd` keeps a single blended figure using
`COST_PER_1K_TOKENS`.

`GET /v1/stats/trend` groups the last 7 days by day, oldest first, as
`{date, total_requests, hit_count, hit_rate}`. Days with no traffic are omitted rather
than reported as zero.

`GET /v1/stats/top-cached` returns the 20 most-hit `cache_entries` with
the similarity range each has served. `min_similarity`/`max_similarity` are `NULL` until
an entry has been served at least once. Note this counts *semantic* hits only — exact
hits are answered from Redis and never touch `cache_entries`.

```bash
curl -s http://localhost:8000/v1/stats -H "Authorization: Bearer dash_demo-tenant-token"
```

## Manual steps before a pilot

Three things this repo cannot do for itself. Each lives in a different place, none is
covered by CI or the test suite, and the deployment is not ready until all three are
done.

| # | Step | Where it is done |
|---|---|---|
| 1 | Apply migration `004` | `psql` against the Railway Postgres |
| 2 | Private networking checklist | Railway dashboard |
| 3 | Set `GATEWAY_DASHBOARD_TOKEN` | Vercel project environment variables |

### 1. Apply migration 004 — manual, via psql

`004_cache_entry_stats.sql` adds the similarity-range columns. It is idempotent, so
re-running it on a database that already has them is a no-op:

```bash
psql $DATABASE_URL -f migrations/004_cache_entry_stats.sql
```

Every migration after `001` is applied for you by `scripts/migrate_hash_keys.py` (see
[Authentication](#authentication)), which also backfills credentials and drops the
plaintext `tenants.api_key` column. That script requires an explicit `--mode=in-place`
or `--mode=rotate`; run it with `--dry-run` first, and run it to completion **before**
restarting the app — see [Deploy order](#deploy-order).

### 2. Private networking checklist — manual, in the Railway dashboard

Railway's networking settings are not something this repo can configure. Before a
pilot, walk this list by hand in the Railway dashboard. Each item is a place where a
datastore can end up on the public internet without anything in the code changing.

- [ ] **Postgres — no public TCP proxy.** Open the Postgres service → **Settings →
      Networking**. If a **TCP Proxy** entry exists (a `*.proxy.rlwy.net` host and a
      port), remove it. That endpoint is reachable from anywhere on the internet and is
      protected by the database password alone.
- [ ] **Redis — no public TCP proxy.** Same check on the Redis service. Redis is the
      more urgent of the two: a Railway Redis has no ACL user beyond the default, so an
      exposed port is a much shorter path in.
- [ ] **`DATABASE_URL` points at the internal host.** On the FastAPI service →
      **Variables**, confirm the host is `postgres.railway.internal` (or Railway's
      `${{Postgres.DATABASE_URL}}` reference, which resolves to it) — *not* a
      `proxy.rlwy.net` host. A public host here means every query leaves the private
      network even though it did not have to.
- [ ] **`REDIS_URL` points at the internal host.** Same check: `redis.railway.internal`
      or `${{Redis.REDIS_URL}}`.
- [ ] **Only the FastAPI service has a public domain.** It is the one thing that should
      be internet-facing. Postgres and Redis should have none.
- [ ] **Env vars are set on the service, not baked into the image.** `.env` is
      gitignored and the Dockerfile does not copy it; confirm nothing has added it.

Verify from the FastAPI service's shell — the internal hostnames only resolve inside
the private network, so this failing from your laptop is the expected result:

```bash
railway run --service <fastapi-service> python -c "import os; print(os.environ['DATABASE_URL'].rsplit('@',1)[-1], os.environ['REDIS_URL'].rsplit('@',1)[-1])"
```

Both should print `*.railway.internal` hosts.

### 3. Set `GATEWAY_DASHBOARD_TOKEN` — manual, in Vercel project settings

The dashboard's server-side proxy (`dashboard/app/gateway/[...path]/route.ts`) attaches
this token to every call it forwards to `/v1/stats*`. Without it the dashboard returns
`500` on every panel — it has no other way to authenticate, since the stats endpoints
stopped accepting a `tenant_id` query parameter.

- **Variable name:** `GATEWAY_DASHBOARD_TOKEN`
- **Where:** Vercel → your project → **Settings → Environment Variables**. This is a
  **project setting, not a file in this repo** — there is nothing to commit, and
  `dashboard/.env.local` is gitignored and applies only to local development.
- **Value:** one of the `dash_...` tokens minted by `scripts/migrate_hash_keys.py`
  during the credential backfill. It is printed **once** and cannot be recovered from
  its hash; if it was not captured, re-run the script to issue a new one.
- **No `NEXT_PUBLIC_` prefix.** That prefix would inline the token into the client
  bundle and publish it to every visitor. It is read at request time on the server.

```bash
vercel env add GATEWAY_DASHBOARD_TOKEN production
```

The token also decides *which tenant* the deployment shows, since the gateway resolves
the tenant from it. Give each pilot partner its own deployment with its own token
rather than sharing one.

Set `NEXT_PUBLIC_GATEWAY_URL` in the same place. Unlike the token, it is read at build
time, so changing it needs a redeploy rather than just a restart.

### Application env vars on the Railway service

```
RATE_LIMIT_PER_MINUTE=100
RATE_LIMIT_WINDOW_SECONDS=60                           # window length; also the Retry-After value
RATE_LIMIT_FAIL_MODE=closed                            # 503 rather than uncapped traffic if Redis drops
DASHBOARD_ORIGIN=https://<your-dashboard>.vercel.app   # only if a browser calls the gateway directly
```

## Tests

```bash
pip install -r requirements.txt pytest pytest-asyncio
pytest tests/
```

Security-relevant coverage lives in:

| File | Covers |
|---|---|
| `tests/test_auth_scopes.py` | Cross-tenant stats reads blocked, `401` on missing/invalid/out-of-scope keys, tokens hashed before they reach SQL |
| `tests/test_rate_limit.py` | `429` past the threshold, per-tenant isolation, fail-open on a Redis outage |
| `tests/test_stats_middleware.py` | CORS allow-list, access-log rows for both accepted and rejected calls |
| `tests/test_startup_schema_check.py` | Startup refuses to serve traffic before the credential backfill has run |
| `tests/test_migrations.py` | Migrations are idempotent, and the scripts glob them rather than naming files |
