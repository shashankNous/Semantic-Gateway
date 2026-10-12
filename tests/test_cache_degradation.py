"""The exact-match cache must degrade, not raise, when Redis is unavailable.

This is what makes RATE_LIMIT_FAIL_MODE=open meaningful: the limiter can decide
to let a request through during a Redis outage, but the request only survives if
the cache lookup immediately after it also tolerates Redis being gone.
"""

import asyncio
import json

import pytest

from app.services import cache


class DeadRedis:
    async def get(self, key):
        raise ConnectionError("redis is down")

    async def setex(self, key, ttl, value):
        raise ConnectionError("redis is down")


class WorkingRedis:
    def __init__(self, values=None):
        self.values = values or {}
        self.writes = []

    async def get(self, key):
        return self.values.get(key)

    async def setex(self, key, ttl, value):
        self.writes.append((key, ttl, value))
        self.values[key] = value


def run(coro):
    return asyncio.run(coro)


def test_a_read_during_an_outage_is_a_miss_not_an_error(monkeypatch):
    monkeypatch.setattr(cache, "redis_client", DeadRedis())

    assert run(cache.get_cached_response("some-key")) is None


def test_a_write_during_an_outage_is_a_no_op_not_an_error(monkeypatch):
    monkeypatch.setattr(cache, "redis_client", DeadRedis())

    # Must not raise; the response was already produced and is being returned.
    assert run(cache.set_cached_response("some-key", {"choices": []})) is None


def test_a_corrupt_entry_is_treated_as_a_miss(monkeypatch):
    monkeypatch.setattr(cache, "redis_client", WorkingRedis({"k": "{not json"}))

    assert run(cache.get_cached_response("k")) is None


def test_the_happy_path_still_round_trips(monkeypatch):
    fake = WorkingRedis()
    monkeypatch.setattr(cache, "redis_client", fake)

    payload = {"choices": [{"message": {"content": "hello"}}]}
    run(cache.set_cached_response("k", payload))

    assert run(cache.get_cached_response("k")) == payload
    assert fake.writes[0][1] == cache.CACHE_TTL_SECONDS
    assert json.loads(fake.writes[0][2]) == payload


def test_a_genuine_miss_is_still_none(monkeypatch):
    monkeypatch.setattr(cache, "redis_client", WorkingRedis())

    assert run(cache.get_cached_response("absent")) is None


def test_proxy_serves_a_request_end_to_end_with_redis_down(monkeypatch):
    """The whole point: fail-open rate limiting plus a degrading cache = a served request."""
    from uuid import uuid4

    from fastapi.testclient import TestClient

    import app.routes.proxy as proxy_module
    from app.main import app
    from app.services import rate_limit

    class DeadCounter:
        async def incr(self, key):
            raise ConnectionError("redis is down")

        async def expire(self, key, seconds):
            raise ConnectionError("redis is down")

        async def ttl(self, key):
            raise ConnectionError("redis is down")

    monkeypatch.setattr(rate_limit, "redis_client", DeadCounter())
    monkeypatch.setattr(rate_limit, "RATE_LIMIT_FAIL_MODE", rate_limit.FAIL_OPEN)
    monkeypatch.setattr(proxy_module, "get_cached_response", cache.get_cached_response)
    monkeypatch.setattr(proxy_module, "set_cached_response", cache.set_cached_response)
    monkeypatch.setattr(cache, "redis_client", DeadRedis())
    monkeypatch.setattr(proxy_module, "MISTRAL_API_KEY", "test-upstream-key")

    async def fake_verify_api_key(request):
        return {"tenant_id": 1, "tenant_uuid": uuid4(), "name": "test", "scope": "proxy"}

    async def fake_call_upstream_llm(body):
        return 200, {"choices": [{"message": {"content": "served"}}], "usage": {}}

    async def noop(*args, **kwargs):
        return None

    async def fake_embed_text(text):
        return [0.1, 0.2]

    monkeypatch.setattr(proxy_module, "verify_api_key", fake_verify_api_key)
    monkeypatch.setattr(proxy_module, "call_upstream_llm", fake_call_upstream_llm)
    monkeypatch.setattr(proxy_module, "find_semantic_match", noop)
    monkeypatch.setattr(proxy_module, "store_semantic_cache_entry", noop)
    monkeypatch.setattr(proxy_module, "log_request_result", noop)
    monkeypatch.setattr(proxy_module, "embed_text", fake_embed_text)

    response = TestClient(app).post(
        "/v1/chat/completions",
        headers={"Authorization": "Bearer sk-test"},
        json={"messages": [{"role": "user", "content": "Explain pgvector"}], "temperature": 0},
    )

    assert response.status_code == 200
    assert response.headers["X-Cache"] == "MISS"
