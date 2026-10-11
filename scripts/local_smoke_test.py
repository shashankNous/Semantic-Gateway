"""End-to-end smoke test of the gateway against the local stack and Ollama.

    python scripts/local_setup.py          # once, or to start over
    python scripts/run_local_gateway.py    # in another terminal
    python scripts/local_smoke_test.py

LOCAL ONLY: reads `.env.local` and `.local-credentials.json`, refuses non-local
URLs, and clears the local cache tables and Redis at the start so it can be re-run.
Every request uses temperature=0 (anything else is uncacheable) and the cache
outcome is read from the gateway's `X-Cache` response header.

Prints one PASS/FAIL line per check. Exits 1 on any FAIL.

Case 8 stops and restarts the local Redis container. Case 9 starts a second
gateway on another port with a deliberately wrong embedding model.
"""

import asyncio
import json
import math
import pathlib
import subprocess
import sys
import time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from scripts._local_env import (  # noqa: E402
    CREDENTIALS_FILE,
    REPO,
    apply_to_process,
    child_env,
    load_local_env,
)

env = load_local_env()
apply_to_process(env)

import asyncpg  # noqa: E402
import httpx  # noqa: E402
from redis import Redis  # noqa: E402

from app.services.match_verification import (  # noqa: E402
    _extract_entities,
    has_entity_mismatch,
    has_polarity_mismatch,
)
from app.services.normalization import normalize_query  # noqa: E402

GATEWAY = f"http://127.0.0.1:{env.get('LOCAL_GATEWAY_PORT', '8010')}"
BROKEN_GATEWAY_PORT = int(env.get("LOCAL_GATEWAY_PORT", "8010")) + 1
THRESHOLD = float(env["SEMANTIC_CACHE_THRESHOLD"])
REDIS_CONTAINER = "sg-local-redis"
MAX_TOKENS = 60  # keeps each phi3 miss to ~15s; part of the cache key, so fixed

results: list[tuple[str, str, str]] = []
similarities: list[tuple[str, float, str]] = []


def record(outcome: str, name: str, detail: str = "") -> None:
    results.append((outcome, name, detail))
    print(f"[{outcome:8}] {name}" + (f"  -- {detail}" if detail else ""), flush=True)


def check(condition: bool, name: str, detail: str = "") -> bool:
    record("PASS" if condition else "FAIL", name, detail)
    return condition


# --- Helpers -----------------------------------------------------------------


def chat(client: httpx.Client, key: str, prompt: str, base: str = GATEWAY):
    started = time.perf_counter()
    response = client.post(
        f"{base}/v1/chat/completions",
        headers={"Authorization": f"Bearer {key}"},
        json={
            "messages": [{"role": "user", "content": prompt}],
            "temperature": 0,
            "max_tokens": MAX_TOKENS,
        },
    )
    elapsed = time.perf_counter() - started
    return response.status_code, response.headers.get("X-Cache"), response, elapsed


def embed(texts: list[str]) -> list[list[float]]:
    response = httpx.post(
        env["EMBEDDING_BASE_URL"].rstrip("/") + "/embeddings",
        headers={"Authorization": f"Bearer {env['EMBEDDING_API_KEY']}"},
        json={"model": env["EMBEDDING_MODEL"], "input": texts},
        timeout=120,
    )
    response.raise_for_status()
    return [item["embedding"] for item in response.json()["data"]]


def similarity(a: str, b: str) -> float:
    """Cosine similarity as the gateway sees it: both sides normalized first."""
    va, vb = embed([normalize_query(a), normalize_query(b)])
    dot = sum(x * y for x, y in zip(va, vb))
    return dot / math.sqrt(sum(x * x for x in va) * sum(y * y for y in vb))


async def db_fetchval(query: str, *args):
    conn = await asyncpg.connect(env["DATABASE_URL"])
    try:
        return await conn.fetchval(query, *args)
    finally:
        await conn.close()


def sql(query: str, *args):
    return asyncio.run(db_fetchval(query, *args))


def tenant_id(name: str) -> int:
    return sql("SELECT id FROM tenants WHERE name = $1", name)


def last_logged_similarity(tid: int) -> float | None:
    return sql(
        "SELECT similarity FROM requests WHERE tenant_id = $1 AND cache_status = 'SEMANTIC_HIT' "
        "ORDER BY id DESC LIMIT 1",
        tid,
    )


def reset_state() -> None:
    sql("TRUNCATE requests, cache_entries, stats_access_log")
    Redis.from_url(env["REDIS_URL"]).flushdb()


def wait_for_health(base: str, seconds: float) -> bool:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        try:
            if httpx.get(f"{base}/health", timeout=2).status_code == 200:
                return True
        except httpx.HTTPError:
            pass
        time.sleep(0.5)
    return False


def wait_for_redis(seconds: float) -> bool:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        try:
            if Redis.from_url(env["REDIS_URL"], socket_timeout=1).ping():
                return True
        except Exception:
            pass
        time.sleep(0.5)
    return False


def verifier_reason(query: str, candidate: str) -> str:
    """Why match verification refuses (or accepts) a pair, in its own terms.

    Same functions, same raw text the gateway verifies, so this is the reason the
    gateway logs as "Skipped unsafe semantic match (...)".
    """
    if has_polarity_mismatch(query, candidate):
        return "verifier: polarity mismatch"
    if has_entity_mismatch(query, candidate):
        return (f"verifier: entity mismatch {sorted(_extract_entities(candidate))} "
                f"vs {sorted(_extract_entities(query))}")
    return "verifier: accepted"


# --- Cases -------------------------------------------------------------------


def near_miss_pair(client, key, first: str, second: str, label: str) -> None:
    """Both prompts must be answered upstream; the second must not reuse the first."""
    status, cache, _, _ = chat(client, key, first)
    check(status == 200 and cache == "MISS", f"5. {label}: first prompt is a miss",
          f"{first!r} -> {status} {cache}")

    score = similarity(first, second)
    status, cache, _, _ = chat(client, key, second)
    if score >= THRESHOLD:
        layer = f"over threshold {THRESHOLD}, so rejected by match verification"
    else:
        layer = f"under threshold {THRESHOLD}, so verification was NOT exercised"
    similarities.append((f"rejected: {label}", score, "MISS" if cache == "MISS" else cache))
    check(status == 200 and cache == "MISS", f"5. {label}: not served from cache",
          f"{second!r} -> {status} {cache}; similarity {score:.4f}, {layer}; "
          f"{verifier_reason(second, first)}")


def shared_entity_pair(client, key, first: str, second: str, label: str) -> None:
    """Pairs that share one entity but differ in another ("July", "API").

    The old any-overlap entity rule served these from each other's cache; the
    sets must now match exactly.
    """
    chat(client, key, first)
    score = similarity(first, second)
    status, cache, _, _ = chat(client, key, second)
    similarities.append((f"shared entity: {label}", score, cache))
    check(status == 200 and cache != "SEMANTIC_HIT",
          f"10b. {label}: not served from cache",
          f"{second!r} -> {status} {cache}; similarity {score:.4f}; "
          f"{verifier_reason(second, first)}")


def redis_outage(client, key) -> None:
    subprocess.run(["docker", "stop", REDIS_CONTAINER], check=True, capture_output=True)
    try:
        status, cache, response, _ = chat(client, key, "What is pgvector?")
        check(status == 503, "8. Redis down: rate limiter fails closed",
              f"-> {status} {response.text[:100]}")
    finally:
        subprocess.run(["docker", "start", REDIS_CONTAINER], check=True, capture_output=True)

    if not wait_for_redis(60):
        record("FAIL", "8. Redis came back", "no PING reply within 60s")
        return
    status, cache, _, _ = chat(client, key, "What is pgvector?")
    check(status == 200, "8. Redis back: requests flow again", f"-> {status} {cache}")


def broken_embeddings(client, key) -> None:
    """A failing embedding service degrades requests to a miss instead of a 500."""
    # 9b needs this prompt in the exact cache; put it there through the working
    # gateway (Redis was restarted in case 8).
    chat(client, key, "What is pgvector?")

    base = f"http://127.0.0.1:{BROKEN_GATEWAY_PORT}"
    log_path = REPO / ".local-broken-gateway.log"
    with log_path.open("w", encoding="utf-8") as log:
        process = subprocess.Popen(
            [
                sys.executable,
                str(REPO / "scripts" / "run_local_gateway.py"),
                f"--port={BROKEN_GATEWAY_PORT}",
                "--set",
                "EMBEDDING_MODEL=does-not-exist-embed",
            ],
            env=child_env(env),
            stdout=log,
            stderr=subprocess.STDOUT,
        )
    try:
        if not wait_for_health(base, 60):
            record("FAIL", "9. Broken-embedding gateway started", f"see {log_path.name}")
            return

        prompt = "What is HNSW indexing in pgvector?"
        status, cache, response, elapsed = chat(client, key, prompt, base=base)
        degraded = response.headers.get("X-Cache-Degraded")
        stored = sql("SELECT count(*) FROM cache_entries WHERE normalized_query = $1",
                     normalize_query(prompt))
        time.sleep(0.5)
        log_text = log_path.read_text(encoding="utf-8", errors="replace")
        warnings = [line for line in log_text.splitlines() if "Semantic cache skipped" in line]
        cause = warnings[-1].strip() if warnings else "no degradation warning logged"
        check(status == 200 and cache == "MISS" and degraded == "embedding-unavailable"
              and stored == 0,
              "9. Embedding failure (wrong model name): degraded miss",
              f"-> HTTP {status}, X-Cache={cache}, X-Cache-Degraded={degraded}, "
              f"cache_entries rows: {stored}, {elapsed:.1f}s; server log: {cause[:170]}")

        status, cache, response, _ = chat(client, key, "What is pgvector?", base=base)
        check(status == 200 and cache == "EXACT_HIT",
              "9b. Embedding failure: exact cache still answers",
              f"-> HTTP {status}, X-Cache={cache}, "
              f"X-Cache-Degraded={response.headers.get('X-Cache-Degraded')}")
    finally:
        process.terminate()
        process.wait(timeout=15)
        log_path.unlink(missing_ok=True)


def main() -> int:
    credentials = json.loads(CREDENTIALS_FILE.read_text(encoding="utf-8"))
    tenant_a = credentials["Local Tenant A"]
    tenant_b = credentials["Local Tenant B"]
    tenant_c = credentials["Local Tenant C"]

    if not wait_for_health(GATEWAY, 5):
        print(f"No gateway at {GATEWAY}. Start it with: python scripts/run_local_gateway.py")
        return 1

    print(f"Gateway {GATEWAY}, upstream {env['UPSTREAM_MODEL']}, embeddings "
          f"{env['EMBEDDING_MODEL']} ({env['EMBEDDING_DIM']} dims), threshold {THRESHOLD}\n")
    reset_state()
    a_id = tenant_id("Local Tenant A")

    with httpx.Client(timeout=180) as client:
        # 1. New prompt
        status, cache, _, elapsed = chat(client, tenant_a["proxy_key"], "What is pgvector?")
        stored = sql("SELECT count(*) FROM cache_entries WHERE normalized_query = 'what is pgvector'")
        check(status == 200 and cache == "MISS" and stored == 1,
              "1. New prompt: miss, completion stored",
              f"-> {status} {cache} in {elapsed:.1f}s; cache_entries rows: {stored}")

        # 2. Same prompt again
        status, cache, _, elapsed = chat(client, tenant_a["proxy_key"], "What is pgvector?")
        check(status == 200 and cache == "EXACT_HIT", "2. Same prompt: exact (Redis) hit",
              f"-> {status} {cache} in {elapsed:.2f}s")

        # 3. Reworded prompt
        reworded = "Can you explain what pgvector is?"
        score = similarity("What is pgvector?", reworded)
        status, cache, _, elapsed = chat(client, tenant_a["proxy_key"], reworded)
        logged = last_logged_similarity(a_id) if cache == "SEMANTIC_HIT" else None
        similarities.append(("hit: reworded pgvector", logged or score, cache))
        check(status == 200 and cache == "SEMANTIC_HIT", "3. Reworded prompt: semantic hit",
              f"-> {status} {cache} in {elapsed:.2f}s; gateway similarity "
              f"{logged if logged is None else round(logged, 4)}, recomputed {score:.4f}")

        # 4. Unrelated prompt
        unrelated = "What is the capital of France?"
        score = similarity("What is pgvector?", unrelated)
        status, cache, _, _ = chat(client, tenant_a["proxy_key"], unrelated)
        similarities.append(("unrelated (vs pgvector)", score, cache))
        check(status == 200 and cache == "MISS", "4. Unrelated prompt: miss",
              f"-> {status} {cache}; similarity to cached entry {score:.4f}")

        # 5. Near-misses that verification must reject
        near_miss_pair(client, tenant_a["proxy_key"], "How do I enable dark mode?",
                       "How do I disable dark mode?", "enable vs disable")
        near_miss_pair(client, tenant_a["proxy_key"],
                       "How hot does the weather get in Delhi during summer?",
                       "How hot does the weather get in Mumbai during summer?",
                       "Delhi vs Mumbai")
        near_miss_pair(client, tenant_a["proxy_key"], "Should we use v2 for this project?",
                       "Should we use v3 for this project?", "v2 vs v3")

        # 6. Tenant isolation
        status, cache, _, _ = chat(client, tenant_b["proxy_key"], "What is pgvector?")
        check(status == 200 and cache == "MISS", "6. Tenant isolation: B misses A's cached prompt",
              f"-> {status} {cache}")

        # 7. Auth separation
        stats_with_proxy = client.get(f"{GATEWAY}/v1/stats",
                                      headers={"Authorization": f"Bearer {tenant_a['proxy_key']}"})
        check(stats_with_proxy.status_code == 401, "7. sk- proxy key rejected by /v1/stats",
              f"-> {stats_with_proxy.status_code}")
        status, _, _, _ = chat(client, tenant_a["dashboard_token"], "What is pgvector?")
        check(status == 401, "7. dash_ token rejected by the proxy", f"-> {status}")
        stats_ok = client.get(f"{GATEWAY}/v1/stats",
                              headers={"Authorization": f"Bearer {tenant_a['dashboard_token']}"})
        check(stats_ok.status_code == 200, "7. dash_ token accepted by /v1/stats (control)",
              f"-> {stats_ok.status_code}, total_requests="
              f"{stats_ok.json().get('total_requests') if stats_ok.status_code == 200 else '?'}")

        # 8. Redis outage
        redis_outage(client, tenant_a["proxy_key"])

        # 9. Embedding failure
        broken_embeddings(client, tenant_a["proxy_key"])

        # 10b. Pairs sharing one entity, on their own tenant
        shared_entity_pair(client, tenant_c["proxy_key"],
                           "What is the weather like in Delhi in July?",
                           "What is the weather like in Mumbai in July?",
                           "Delhi vs Mumbai, shared 'July'")
        shared_entity_pair(client, tenant_c["proxy_key"], "Should I use v2 of the API?",
                           "Should I use v3 of the API?", "v2 vs v3, shared 'API'")

    # 10. Score distribution
    print(f"\n10. Similarity scores (threshold {THRESHOLD}):")
    for label, score, cache in sorted(similarities, key=lambda item: -item[1]):
        side = ">=" if score >= THRESHOLD else "< "
        print(f"    {score:.4f} {side} threshold  {cache or '-':13} {label}")

    failed = [r for r in results if r[0] == "FAIL"]
    passed = [r for r in results if r[0] == "PASS"]
    print(f"\n{len(passed)} passed, {len(failed)} failed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
