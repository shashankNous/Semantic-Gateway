import asyncio
import json
from uuid import uuid4

from app.services.cacheability import is_cacheable
from app.services.normalization import normalize_query
from app.services import semantic_cache
import app.routes.proxy as proxy_module


class FakeRequest:
    headers = {"Authorization": "Bearer test-key"}

    def __init__(self, body):
        self._body = body

    async def json(self):
        return self._body


class FakeAcquire:
    def __init__(self, conn):
        self.conn = conn

    async def __aenter__(self):
        return self.conn

    async def __aexit__(self, exc_type, exc, tb):
        return None


class FakePool:
    def __init__(self, conn):
        self.conn = conn

    def acquire(self):
        return FakeAcquire(self.conn)


class FakeSemanticConn:
    def __init__(self, rows):
        self.rows = rows
        self.executed = []

    async def fetch(self, *args):
        return self.rows

    async def execute(self, *args):
        self.executed.append(args)


def run(coro):
    return asyncio.run(coro)


def chat_body(query):
    return {
        "model": "mistral-small-latest",
        "messages": [{"role": "user", "content": query}],
        "temperature": 0,
    }


def response_json(response):
    return json.loads(response.body.decode("utf-8"))


def test_normalize_query():
    assert normalize_query("Hello,   WHAT is Semantic Cache???") == "what is semantic cache"
    assert normalize_query("Good morning!  Tell me about vectors.") == "tell me about vectors"


def test_is_cacheable():
    assert is_cacheable("Explain pgvector cosine search") is True
    assert is_cacheable("Where is my order?") is False
    assert is_cacheable("What is the latest pricing?") is False
    assert is_cacheable("Lookup transaction id TXN-ABC123456789") is False


def test_exact_cache_is_checked_before_semantic_cache(monkeypatch):
    calls = []

    async def fake_verify_api_key(request):
        return {"tenant_id": 1, "tenant_uuid": uuid4(), "name": "test"}

    async def fake_get_cached_response(key):
        calls.append("redis")
        return {"choices": [{"message": {"content": "exact"}}]}

    async def fail_semantic(*args, **kwargs):
        raise AssertionError("semantic cache should not be checked on exact hit")

    async def fake_log(*args, **kwargs):
        calls.append("log")

    monkeypatch.setattr(proxy_module, "MISTRAL_API_KEY", "test")
    monkeypatch.setattr(proxy_module, "verify_api_key", fake_verify_api_key)
    monkeypatch.setattr(proxy_module, "get_cached_response", fake_get_cached_response)
    monkeypatch.setattr(proxy_module, "find_semantic_match", fail_semantic)
    monkeypatch.setattr(proxy_module, "embed_text", fail_semantic)
    monkeypatch.setattr(proxy_module, "call_upstream_llm", fail_semantic)
    monkeypatch.setattr(proxy_module, "log_request_result", fake_log)

    response = run(proxy_module.proxy(FakeRequest(chat_body("Explain vector search"))))

    assert response.status_code == 200
    assert response.headers["X-Cache"] == "EXACT_HIT"
    assert response_json(response)["choices"][0]["message"]["content"] == "exact"
    assert calls == ["redis", "log"]


def test_semantic_cache_returns_only_above_threshold(monkeypatch):
    tenant_id = uuid4()
    hit_row = {
        "id": uuid4(),
        "response_json": {"ok": True},
        "normalized_query": "what is caching",
        "similarity": 0.93,
    }
    hit_conn = FakeSemanticConn([hit_row])
    monkeypatch.setattr(semantic_cache.db, "pool", FakePool(hit_conn))

    hit = run(semantic_cache.find_semantic_match(tenant_id, [0.1, 0.2], threshold=0.92))

    assert hit["response_json"] == {"ok": True}
    assert hit["similarity"] == 0.93
    assert len(hit_conn.executed) == 1

    miss_conn = FakeSemanticConn([{**hit_row, "similarity": 0.91}])
    monkeypatch.setattr(semantic_cache.db, "pool", FakePool(miss_conn))

    miss = run(semantic_cache.find_semantic_match(tenant_id, [0.1, 0.2], threshold=0.92))

    assert miss is None
    assert miss_conn.executed == []


def test_uncacheable_queries_bypass_redis_and_semantic_cache(monkeypatch):
    async def fake_verify_api_key(request):
        return {"tenant_id": 1, "tenant_uuid": uuid4(), "name": "test"}

    async def fail_cache(*args, **kwargs):
        raise AssertionError("cache should be bypassed for uncacheable query")

    async def fake_call_upstream(body):
        return 200, {"choices": [{"message": {"content": "fresh"}}]}

    async def fake_log(*args, **kwargs):
        return None

    monkeypatch.setattr(proxy_module, "MISTRAL_API_KEY", "test")
    monkeypatch.setattr(proxy_module, "verify_api_key", fake_verify_api_key)
    monkeypatch.setattr(proxy_module, "get_cached_response", fail_cache)
    monkeypatch.setattr(proxy_module, "find_semantic_match", fail_cache)
    monkeypatch.setattr(proxy_module, "embed_text", fail_cache)
    monkeypatch.setattr(proxy_module, "call_upstream_llm", fake_call_upstream)
    monkeypatch.setattr(proxy_module, "log_request_result", fake_log)

    response = run(proxy_module.proxy(FakeRequest(chat_body("Where is my order?"))))

    assert response.status_code == 200
    assert response.headers["X-Cache"] == "UNCACHEABLE"
    assert response_json(response)["choices"][0]["message"]["content"] == "fresh"


def test_semantic_hit_after_redis_miss(monkeypatch):
    logged = {}

    async def fake_verify_api_key(request):
        return {"tenant_id": 1, "tenant_uuid": uuid4(), "name": "test"}

    async def fake_get_cached_response(key):
        return None

    async def fake_embed_text(text):
        return [0.1, 0.2, 0.3]

    async def fake_find_semantic_match(tenant_uuid, embedding, *args, **kwargs):
        return {
            "id": uuid4(),
            "response_json": {"choices": [{"message": {"content": "semantic"}}]},
            "normalized_query": "what is caching",
            "similarity": 0.95,
        }

    async def fail_upstream(*args, **kwargs):
        raise AssertionError("upstream should not be called on semantic hit")

    async def fake_log(**kwargs):
        logged.update(kwargs)

    monkeypatch.setattr(proxy_module, "MISTRAL_API_KEY", "test")
    monkeypatch.setattr(proxy_module, "verify_api_key", fake_verify_api_key)
    monkeypatch.setattr(proxy_module, "get_cached_response", fake_get_cached_response)
    monkeypatch.setattr(proxy_module, "embed_text", fake_embed_text)
    monkeypatch.setattr(proxy_module, "find_semantic_match", fake_find_semantic_match)
    monkeypatch.setattr(proxy_module, "call_upstream_llm", fail_upstream)
    monkeypatch.setattr(proxy_module, "log_request_result", fake_log)

    response = run(proxy_module.proxy(FakeRequest(chat_body("What is caching?"))))

    assert response.status_code == 200
    assert response.headers["X-Cache"] == "SEMANTIC_HIT"
    assert response_json(response)["choices"][0]["message"]["content"] == "semantic"
    assert logged["cache_status"] == "SEMANTIC_HIT"
    assert logged["similarity"] == 0.95


def test_polarity_opposite_query_misses_instead_of_semantic_hit(monkeypatch):
    """A seeded "enable dark mode" entry must not answer a "disable dark mode" ask."""
    logged = {}
    seeded_row = {
        "id": uuid4(),
        "response_json": {"choices": [{"message": {"content": "here is how to enable dark mode"}}]},
        "normalized_query": "how do i enable dark mode",
        "similarity": 0.97,
    }
    seeded_conn = FakeSemanticConn([seeded_row])
    monkeypatch.setattr(semantic_cache.db, "pool", FakePool(seeded_conn))

    async def fake_verify_api_key(request):
        return {"tenant_id": 1, "tenant_uuid": uuid4(), "name": "test"}

    async def fake_get_cached_response(key):
        return None

    async def fake_embed_text(text):
        return [0.1, 0.2, 0.3]

    async def fake_call_upstream(body):
        return 200, {"choices": [{"message": {"content": "here is how to disable dark mode"}}]}

    async def fake_set_cached_response(key, response):
        return None

    async def fake_store_semantic_cache_entry(*args, **kwargs):
        return None

    async def fake_log(**kwargs):
        logged.update(kwargs)

    monkeypatch.setattr(proxy_module, "MISTRAL_API_KEY", "test")
    monkeypatch.setattr(proxy_module, "verify_api_key", fake_verify_api_key)
    monkeypatch.setattr(proxy_module, "get_cached_response", fake_get_cached_response)
    monkeypatch.setattr(proxy_module, "embed_text", fake_embed_text)
    monkeypatch.setattr(proxy_module, "call_upstream_llm", fake_call_upstream)
    monkeypatch.setattr(proxy_module, "set_cached_response", fake_set_cached_response)
    monkeypatch.setattr(proxy_module, "store_semantic_cache_entry", fake_store_semantic_cache_entry)
    monkeypatch.setattr(proxy_module, "log_request_result", fake_log)

    response = run(proxy_module.proxy(FakeRequest(chat_body("How do I disable dark mode?"))))

    assert response.status_code == 200
    assert response.headers["X-Cache"] == "MISS"
    assert response_json(response)["choices"][0]["message"]["content"] == "here is how to disable dark mode"
    assert logged["cache_status"] == "MISS"
    # The rejected candidate must not have had its hit_count bumped.
    assert seeded_conn.executed == []


def test_entity_swap_uses_raw_query_and_misses(monkeypatch):
    """Capitalized entities survive via raw_query, so a city swap is rejected."""
    logged = {}
    seeded_row = {
        "id": uuid4(),
        "response_json": {"choices": [{"message": {"content": "sunny in Delhi"}}]},
        "normalized_query": "what is the weather in delhi",
        "raw_query": "What is the weather in Delhi",
        "similarity": 0.96,
    }
    seeded_conn = FakeSemanticConn([seeded_row])
    monkeypatch.setattr(semantic_cache.db, "pool", FakePool(seeded_conn))

    async def fake_verify_api_key(request):
        return {"tenant_id": 1, "tenant_uuid": uuid4(), "name": "test"}

    async def fake_get_cached_response(key):
        return None

    async def fake_embed_text(text):
        return [0.1, 0.2, 0.3]

    async def fake_call_upstream(body):
        return 200, {"choices": [{"message": {"content": "humid in Mumbai"}}]}

    async def fake_noop(*args, **kwargs):
        return None

    async def fake_log(**kwargs):
        logged.update(kwargs)

    monkeypatch.setattr(proxy_module, "MISTRAL_API_KEY", "test")
    monkeypatch.setattr(proxy_module, "verify_api_key", fake_verify_api_key)
    monkeypatch.setattr(proxy_module, "get_cached_response", fake_get_cached_response)
    monkeypatch.setattr(proxy_module, "embed_text", fake_embed_text)
    monkeypatch.setattr(proxy_module, "call_upstream_llm", fake_call_upstream)
    monkeypatch.setattr(proxy_module, "set_cached_response", fake_noop)
    monkeypatch.setattr(proxy_module, "store_semantic_cache_entry", fake_noop)
    monkeypatch.setattr(proxy_module, "log_request_result", fake_log)

    response = run(proxy_module.proxy(FakeRequest(chat_body("What is the weather in Mumbai"))))

    assert response.headers["X-Cache"] == "MISS"
    assert response_json(response)["choices"][0]["message"]["content"] == "humid in Mumbai"
    assert logged["cache_status"] == "MISS"
    assert seeded_conn.executed == []


def test_entity_check_falls_back_to_normalized_query_for_legacy_rows(monkeypatch):
    """Pre-migration rows have no raw_query; the number check must still apply."""
    tenant_id = uuid4()
    legacy_row = {
        "id": uuid4(),
        "response_json": {"ok": True},
        "normalized_query": "how do i upgrade to version 2",
        "raw_query": None,
        "similarity": 0.97,
    }
    conn = FakeSemanticConn([legacy_row])
    monkeypatch.setattr(semantic_cache.db, "pool", FakePool(conn))

    miss = run(
        semantic_cache.find_semantic_match(
            tenant_id,
            [0.1, 0.2],
            "how do i upgrade to version 3",
            raw_query="How do I upgrade to version 3",
        )
    )

    assert miss is None
    assert conn.executed == []


def test_semantically_close_rephrasing_still_hits(monkeypatch):
    """The guardrail must not block a genuine rephrasing of the same question."""
    tenant_id = uuid4()
    seeded_row = {
        "id": uuid4(),
        "response_json": {"ok": True},
        "normalized_query": "how do i enable dark mode",
        "similarity": 0.95,
    }
    conn = FakeSemanticConn([seeded_row])
    monkeypatch.setattr(semantic_cache.db, "pool", FakePool(conn))

    hit = run(
        semantic_cache.find_semantic_match(
            tenant_id, [0.1, 0.2], "how do i switch on dark mode", threshold=0.92
        )
    )

    assert hit is not None
    assert hit["response_json"] == {"ok": True}
    assert len(conn.executed) == 1


def test_unsafe_candidate_is_skipped_for_next_safe_candidate(monkeypatch):
    """An unsafe top candidate falls through to the next one, not straight to a miss."""
    tenant_id = uuid4()
    rows = [
        {
            "id": uuid4(),
            "response_json": {"which": "unsafe"},
            "normalized_query": "how do i disable dark mode",
            "similarity": 0.98,
        },
        {
            "id": uuid4(),
            "response_json": {"which": "safe"},
            "normalized_query": "how do i switch on dark mode",
            "similarity": 0.94,
        },
    ]
    conn = FakeSemanticConn(rows)
    monkeypatch.setattr(semantic_cache.db, "pool", FakePool(conn))

    hit = run(
        semantic_cache.find_semantic_match(
            tenant_id, [0.1, 0.2], "how do i enable dark mode", threshold=0.92
        )
    )

    assert hit["response_json"] == {"which": "safe"}
    assert hit["similarity"] == 0.94


def test_true_miss_calls_upstream_and_stores_in_both_caches(monkeypatch):
    stored = {}

    async def fake_verify_api_key(request):
        return {"tenant_id": 1, "tenant_uuid": uuid4(), "name": "test"}

    async def fake_get_cached_response(key):
        return None

    async def fake_embed_text(text):
        return [0.4, 0.5, 0.6]

    async def fake_find_semantic_match(tenant_uuid, embedding, *args, **kwargs):
        return None

    async def fake_call_upstream(body):
        return 200, {"choices": [{"message": {"content": "fresh"}}]}

    async def fake_set_cached_response(key, response):
        stored["redis"] = response

    async def fake_store_semantic_cache_entry(
        tenant_uuid, normalized_query, prompt_hash, response_json, embedding, model=None, raw_query=None
    ):
        stored["semantic"] = response_json
        stored["normalized_query"] = normalized_query
        stored["raw_query"] = raw_query

    async def fake_log(**kwargs):
        stored["cache_status"] = kwargs.get("cache_status")

    monkeypatch.setattr(proxy_module, "MISTRAL_API_KEY", "test")
    monkeypatch.setattr(proxy_module, "verify_api_key", fake_verify_api_key)
    monkeypatch.setattr(proxy_module, "get_cached_response", fake_get_cached_response)
    monkeypatch.setattr(proxy_module, "embed_text", fake_embed_text)
    monkeypatch.setattr(proxy_module, "find_semantic_match", fake_find_semantic_match)
    monkeypatch.setattr(proxy_module, "call_upstream_llm", fake_call_upstream)
    monkeypatch.setattr(proxy_module, "set_cached_response", fake_set_cached_response)
    monkeypatch.setattr(proxy_module, "store_semantic_cache_entry", fake_store_semantic_cache_entry)
    monkeypatch.setattr(proxy_module, "log_request_result", fake_log)

    response = run(proxy_module.proxy(FakeRequest(chat_body("Explain pgvector cosine search"))))

    assert response.status_code == 200
    assert response.headers["X-Cache"] == "MISS"
    assert stored["redis"]["choices"][0]["message"]["content"] == "fresh"
    assert stored["semantic"]["choices"][0]["message"]["content"] == "fresh"
    assert stored["cache_status"] == "MISS"
    # Both forms are persisted: normalized for embedding/matching, raw for verification.
    assert stored["normalized_query"] == "explain pgvector cosine search"
    assert stored["raw_query"] == "Explain pgvector cosine search"
