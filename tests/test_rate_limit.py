"""Per-tenant throttling on the proxy route."""

import asyncio
from uuid import uuid4

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

import app.routes.proxy as proxy_module
from app.main import app
from app.services import rate_limit


class FakeRedis:
    """Enough of the Redis interface for INCR/EXPIRE/TTL, per key."""

    def __init__(self, fail=False):
        self.counts = {}
        self.ttls = {}
        self.fail = fail
        self.expire_calls = []

    async def incr(self, key):
        if self.fail:
            raise ConnectionError("redis is down")
        self.counts[key] = self.counts.get(key, 0) + 1
        self.ttls.setdefault(key, -1)
        return self.counts[key]

    async def expire(self, key, seconds):
        self.expire_calls.append((key, seconds))
        self.ttls[key] = seconds
        return True

    async def ttl(self, key):
        return self.ttls.get(key, -2)


def run(coro):
    return asyncio.run(coro)


@pytest.fixture
def redis(monkeypatch):
    fake = FakeRedis()
    monkeypatch.setattr(rate_limit, "redis_client", fake)
    return fake


def test_requests_under_the_limit_are_allowed(redis):
    for expected in range(1, 4):
        assert run(rate_limit.enforce_rate_limit(1, limit=3)) == expected


def test_exceeding_the_limit_returns_429(redis):
    for _ in range(3):
        run(rate_limit.enforce_rate_limit(1, limit=3))

    with pytest.raises(HTTPException) as excinfo:
        run(rate_limit.enforce_rate_limit(1, limit=3))

    assert excinfo.value.status_code == 429
    assert "Rate limit exceeded" in excinfo.value.detail
    assert excinfo.value.headers["Retry-After"] == str(rate_limit.RATE_LIMIT_WINDOW_SECONDS)


def test_the_window_is_armed_once_per_window(redis):
    for _ in range(3):
        run(rate_limit.enforce_rate_limit(1, limit=10))

    # EXPIRE on the first INCR only; the TTL must not be pushed forward by later
    # requests, or a busy tenant would never see its window roll over.
    assert redis.expire_calls == [("tenant:1:count", rate_limit.RATE_LIMIT_WINDOW_SECONDS)]


def test_a_key_that_lost_its_ttl_is_rearmed(redis, monkeypatch):
    run(rate_limit.enforce_rate_limit(1, limit=10))
    redis.ttls["tenant:1:count"] = -1  # e.g. restored from a snapshot without TTLs

    run(rate_limit.enforce_rate_limit(1, limit=10))

    assert len(redis.expire_calls) == 2


def test_tenants_are_counted_separately(redis):
    for _ in range(3):
        run(rate_limit.enforce_rate_limit(1, limit=3))

    # Tenant 1 is at its limit; tenant 2 must be unaffected.
    assert run(rate_limit.enforce_rate_limit(2, limit=3)) == 1
    assert redis.counts == {"tenant:1:count": 3, "tenant:2:count": 1}


def test_limit_is_configurable_and_zero_disables_it(redis):
    for _ in range(50):
        assert run(rate_limit.enforce_rate_limit(1, limit=0)) == 0

    assert redis.counts == {}


def test_default_limit_comes_from_the_env_var(monkeypatch, redis):
    monkeypatch.setattr(rate_limit, "RATE_LIMIT_PER_MINUTE", 2)

    run(rate_limit.enforce_rate_limit(1))
    run(rate_limit.enforce_rate_limit(1))

    with pytest.raises(HTTPException) as excinfo:
        run(rate_limit.enforce_rate_limit(1))

    assert excinfo.value.status_code == 429


# --- Redis-outage behaviour (RATE_LIMIT_FAIL_MODE) ----------------------------


@pytest.fixture
def dead_redis(monkeypatch):
    """Every counter command raises, as if Redis were unreachable."""
    fake = FakeRedis(fail=True)
    monkeypatch.setattr(rate_limit, "redis_client", fake)
    return fake


def test_the_default_fail_mode_is_closed():
    """The default is the security-preserving one, not the available one."""
    assert rate_limit.RATE_LIMIT_FAIL_MODE == rate_limit.FAIL_CLOSED


def test_an_invalid_fail_mode_is_rejected_rather_than_guessed(monkeypatch):
    """A typo must not silently pick a posture the operator did not ask for."""
    import importlib

    monkeypatch.setenv("RATE_LIMIT_FAIL_MODE", "close")  # note: not "closed"

    with pytest.raises(RuntimeError, match="RATE_LIMIT_FAIL_MODE"):
        importlib.reload(rate_limit)

    # Leave the module as the rest of the suite expects it.
    monkeypatch.delenv("RATE_LIMIT_FAIL_MODE")
    importlib.reload(rate_limit)


def test_closed_mode_rejects_with_503_when_redis_is_down(monkeypatch, dead_redis):
    monkeypatch.setattr(rate_limit, "RATE_LIMIT_FAIL_MODE", rate_limit.FAIL_CLOSED)

    with pytest.raises(HTTPException) as excinfo:
        run(rate_limit.enforce_rate_limit(1, limit=100))

    assert excinfo.value.status_code == 503
    assert "unavailable" in excinfo.value.detail
    assert excinfo.value.headers["Retry-After"] == str(rate_limit.RATE_LIMIT_WINDOW_SECONDS)


def test_open_mode_passes_the_request_through_when_redis_is_down(monkeypatch, dead_redis):
    monkeypatch.setattr(rate_limit, "RATE_LIMIT_FAIL_MODE", rate_limit.FAIL_OPEN)

    # Uncapped: the limit is 1, but nothing is counting, so nothing is rejected.
    for _ in range(5):
        assert run(rate_limit.enforce_rate_limit(1, limit=1)) == 0


def test_a_disabled_limiter_ignores_the_fail_mode(monkeypatch, dead_redis):
    """limit<=0 short-circuits before Redis, so closed mode must not 503."""
    monkeypatch.setattr(rate_limit, "RATE_LIMIT_FAIL_MODE", rate_limit.FAIL_CLOSED)

    assert run(rate_limit.enforce_rate_limit(1, limit=0)) == 0


@pytest.mark.parametrize(
    "mode,expected_status",
    [(rate_limit.FAIL_CLOSED, 503), (rate_limit.FAIL_OPEN, 200)],
)
def test_proxy_route_honours_the_fail_mode_during_an_outage(
    monkeypatch, dead_redis, mode, expected_status
):
    """The same two modes, through the real route."""
    monkeypatch.setattr(rate_limit, "RATE_LIMIT_FAIL_MODE", mode)
    monkeypatch.setattr(proxy_module, "MISTRAL_API_KEY", "test-upstream-key")

    upstream_calls = []

    async def fake_verify_api_key(request):
        return {"tenant_id": 1, "tenant_uuid": uuid4(), "name": "test", "scope": "proxy"}

    async def fake_call_upstream_llm(body):
        upstream_calls.append(body)
        return 200, {"choices": [{"message": {"content": "hi"}}], "usage": {}}

    async def noop(*args, **kwargs):
        return None

    async def fake_embed_text(text):
        return [0.1, 0.2]

    monkeypatch.setattr(proxy_module, "verify_api_key", fake_verify_api_key)
    monkeypatch.setattr(proxy_module, "call_upstream_llm", fake_call_upstream_llm)
    monkeypatch.setattr(proxy_module, "get_cached_response", noop)
    monkeypatch.setattr(proxy_module, "set_cached_response", noop)
    monkeypatch.setattr(proxy_module, "find_semantic_match", noop)
    monkeypatch.setattr(proxy_module, "store_semantic_cache_entry", noop)
    monkeypatch.setattr(proxy_module, "log_request_result", noop)
    monkeypatch.setattr(proxy_module, "embed_text", fake_embed_text)

    response = TestClient(app).post(
        "/v1/chat/completions",
        headers={"Authorization": "Bearer sk-test"},
        json={"messages": [{"role": "user", "content": "Explain pgvector"}], "temperature": 0},
    )

    assert response.status_code == expected_status
    # Closed mode must reject before spending money upstream.
    assert len(upstream_calls) == (0 if mode == rate_limit.FAIL_CLOSED else 1)


def test_proxy_route_returns_429_over_the_threshold(monkeypatch, redis):
    """The same thing again, but through the real route, headers and all."""
    monkeypatch.setattr(rate_limit, "RATE_LIMIT_PER_MINUTE", 2)
    monkeypatch.setattr(proxy_module, "MISTRAL_API_KEY", "test-upstream-key")

    async def fake_verify_api_key(request):
        return {"tenant_id": 1, "tenant_uuid": uuid4(), "name": "test", "scope": "proxy"}

    upstream_calls = []

    async def fake_call_upstream_llm(body):
        upstream_calls.append(body)
        return 200, {"choices": [{"message": {"content": "hi"}}], "usage": {}}

    async def noop(*args, **kwargs):
        return None

    monkeypatch.setattr(proxy_module, "verify_api_key", fake_verify_api_key)
    monkeypatch.setattr(proxy_module, "call_upstream_llm", fake_call_upstream_llm)
    monkeypatch.setattr(proxy_module, "get_cached_response", noop)
    monkeypatch.setattr(proxy_module, "set_cached_response", noop)
    monkeypatch.setattr(proxy_module, "find_semantic_match", noop)
    monkeypatch.setattr(proxy_module, "store_semantic_cache_entry", noop)
    monkeypatch.setattr(proxy_module, "log_request_result", noop)

    async def fake_embed_text(text):
        return [0.1, 0.2]

    monkeypatch.setattr(proxy_module, "embed_text", fake_embed_text)

    client = TestClient(app)
    body = {"messages": [{"role": "user", "content": "Explain pgvector"}], "temperature": 0}
    headers = {"Authorization": "Bearer sk-test"}

    assert client.post("/v1/chat/completions", json=body, headers=headers).status_code == 200
    assert client.post("/v1/chat/completions", json=body, headers=headers).status_code == 200

    throttled = client.post("/v1/chat/completions", json=body, headers=headers)

    assert throttled.status_code == 429
    assert throttled.headers["Retry-After"] == str(rate_limit.RATE_LIMIT_WINDOW_SECONDS)
    # Throttling happens before the upstream call, so the third request costs nothing.
    assert len(upstream_calls) == 2
