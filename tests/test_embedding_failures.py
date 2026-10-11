"""An embedding failure degrades the request to a plain miss instead of a 500.

The semantic cache is an optimisation in front of the completion model, so losing
the embedding service (down, wrong model name, timeout, garbage response) must not
take the gateway down with it. Found by case 9 of scripts/local_smoke_test.py,
which got an unhandled 500 from a wrong EMBEDDING_MODEL.
"""

import asyncio
import json
from uuid import uuid4

import httpx
import pytest

import app.routes.proxy as proxy_module
from app.services import embeddings
from app.services.embeddings import EmbeddingUnavailableError, embed_text


def run(coro):
    return asyncio.run(coro)


# --- embed_text() raises one exception type for every failure ------------------


@pytest.fixture
def embedding_transport(monkeypatch):
    """Route embed_text()'s HTTP call through a handler the test supplies."""
    monkeypatch.setenv("EMBEDDING_API_KEY", "test-embedding-key")
    real_client = httpx.AsyncClient

    def install(handler):
        def client(*args, **kwargs):
            return real_client(*args, transport=httpx.MockTransport(handler), **kwargs)

        monkeypatch.setattr(embeddings.httpx, "AsyncClient", client)

    return install


def embedding_body(vector):
    return {"data": [{"embedding": vector}]}


def test_a_well_formed_response_returns_the_vector(embedding_transport):
    vector = [0.1] * embeddings.EMBEDDING_DIM
    embedding_transport(lambda request: httpx.Response(200, json=embedding_body(vector)))

    assert run(embed_text("hello")) == vector


def test_a_connection_error_raises_embedding_unavailable(embedding_transport):
    def refuse(request):
        raise httpx.ConnectError("connection refused", request=request)

    embedding_transport(refuse)

    with pytest.raises(EmbeddingUnavailableError, match="ConnectError"):
        run(embed_text("hello"))


def test_a_timeout_raises_embedding_unavailable(embedding_transport):
    def hang(request):
        raise httpx.ReadTimeout("timed out", request=request)

    embedding_transport(hang)

    with pytest.raises(EmbeddingUnavailableError, match="ReadTimeout"):
        run(embed_text("hello"))


def test_a_404_raises_embedding_unavailable(embedding_transport):
    """What Ollama returns for an EMBEDDING_MODEL it does not have."""
    embedding_transport(
        lambda request: httpx.Response(404, json={"error": "model 'nope' not found"})
    )

    with pytest.raises(EmbeddingUnavailableError, match="404"):
        run(embed_text("hello"))


@pytest.mark.parametrize(
    "body",
    [
        {"data": []},
        {"unexpected": True},
        {"data": [{"embedding": "not a list"}]},
        "not json at all",
    ],
    ids=["empty-data", "no-data-key", "embedding-not-a-list", "not-json"],
)
def test_a_malformed_response_raises_embedding_unavailable(embedding_transport, body):
    if isinstance(body, str):
        embedding_transport(lambda request: httpx.Response(200, text=body))
    else:
        embedding_transport(lambda request: httpx.Response(200, json=body))

    with pytest.raises(EmbeddingUnavailableError):
        run(embed_text("hello"))


def test_a_missing_api_key_raises_embedding_unavailable(monkeypatch):
    monkeypatch.delenv("EMBEDDING_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)

    with pytest.raises(EmbeddingUnavailableError, match="API key"):
        run(embed_text("hello"))


def test_the_error_message_never_contains_the_api_key(embedding_transport):
    embedding_transport(lambda request: httpx.Response(401, json={"error": "bad key"}))

    with pytest.raises(EmbeddingUnavailableError) as excinfo:
        run(embed_text("hello"))

    assert "test-embedding-key" not in str(excinfo.value)


def test_cancellation_is_not_swallowed(embedding_transport):
    def cancel(request):
        raise asyncio.CancelledError()

    embedding_transport(cancel)

    with pytest.raises(asyncio.CancelledError):
        run(embed_text("hello"))


# --- The proxy route degrades to a miss ---------------------------------------


class FakeRequest:
    headers = {"Authorization": "Bearer test-key"}

    def __init__(self, body):
        self._body = body

    async def json(self):
        return self._body


def chat_body(query):
    return {"messages": [{"role": "user", "content": query}], "temperature": 0}


@pytest.fixture
def degraded_route(monkeypatch):
    """Proxy wiring with a failing embedder; returns what each dependency saw."""
    seen = {"redis_writes": [], "semantic_writes": [], "logs": [], "semantic_lookups": 0}
    exact_cache = {}

    async def fake_verify_api_key(request):
        return {"tenant_id": 1, "tenant_uuid": uuid4(), "name": "test"}

    async def fake_get_cached_response(key):
        return exact_cache.get(key)

    async def failing_embed_text(text):
        raise EmbeddingUnavailableError("embedding call failed: ConnectError")

    async def fake_find_semantic_match(*args, **kwargs):
        seen["semantic_lookups"] += 1
        return None

    async def fake_call_upstream(body):
        return 200, {"choices": [{"message": {"content": "fresh"}}]}

    async def fake_set_cached_response(key, response):
        seen["redis_writes"].append(response)
        exact_cache[key] = response

    async def fake_store_semantic_cache_entry(*args, **kwargs):
        seen["semantic_writes"].append(args)

    async def fake_log(**kwargs):
        seen["logs"].append(kwargs)

    monkeypatch.setattr(proxy_module, "MISTRAL_API_KEY", "test")
    monkeypatch.setattr(proxy_module, "verify_api_key", fake_verify_api_key)
    monkeypatch.setattr(proxy_module, "get_cached_response", fake_get_cached_response)
    monkeypatch.setattr(proxy_module, "embed_text", failing_embed_text)
    monkeypatch.setattr(proxy_module, "find_semantic_match", fake_find_semantic_match)
    monkeypatch.setattr(proxy_module, "call_upstream_llm", fake_call_upstream)
    monkeypatch.setattr(proxy_module, "set_cached_response", fake_set_cached_response)
    monkeypatch.setattr(proxy_module, "store_semantic_cache_entry", fake_store_semantic_cache_entry)
    monkeypatch.setattr(proxy_module, "log_request_result", fake_log)

    return seen


def test_an_embedding_failure_is_answered_as_a_degraded_miss(degraded_route):
    response = run(proxy_module.proxy(FakeRequest(chat_body("What is pgvector?"))))

    assert response.status_code == 200
    assert json.loads(response.body)["choices"][0]["message"]["content"] == "fresh"
    assert response.headers["X-Cache"] == "MISS"
    assert response.headers["X-Cache-Degraded"] == "embedding-unavailable"

    # One requests row, as a plain miss.
    assert [log["cache_status"] for log in degraded_route["logs"]] == ["MISS"]
    # No embedding: no pgvector lookup and no cache_entries row ...
    assert degraded_route["semantic_lookups"] == 0
    assert degraded_route["semantic_writes"] == []
    # ... but the exact cache needs none, so it is still written.
    assert len(degraded_route["redis_writes"]) == 1


def test_an_exact_hit_still_works_while_embeddings_are_down(degraded_route):
    run(proxy_module.proxy(FakeRequest(chat_body("What is pgvector?"))))

    response = run(proxy_module.proxy(FakeRequest(chat_body("What is pgvector?"))))

    assert response.status_code == 200
    assert response.headers["X-Cache"] == "EXACT_HIT"
    assert "X-Cache-Degraded" not in response.headers


def test_a_healthy_miss_carries_no_degraded_header(degraded_route, monkeypatch):
    async def working_embed_text(text):
        return [0.1, 0.2]

    monkeypatch.setattr(proxy_module, "embed_text", working_embed_text)

    response = run(proxy_module.proxy(FakeRequest(chat_body("What is pgvector?"))))

    assert response.headers["X-Cache"] == "MISS"
    assert "X-Cache-Degraded" not in response.headers
    assert len(degraded_route["semantic_writes"]) == 1


def test_the_degradation_is_logged_once_with_the_exception_type(degraded_route, caplog):
    with caplog.at_level("WARNING", logger=proxy_module.__name__):
        run(proxy_module.proxy(FakeRequest(chat_body("What is pgvector?"))))

    records = [r for r in caplog.records if "embedding" in r.getMessage().lower()]
    assert len(records) == 1
    assert "EmbeddingUnavailableError" in records[0].getMessage()


# --- Fix 4: the returned vector must be EMBEDDING_DIM wide -----------------------


def test_a_wrong_length_embedding_raises_embedding_unavailable(embedding_transport):
    """Caught here, before upstream is paid, instead of at the cache_entries INSERT."""
    wrong = [0.1] * (embeddings.EMBEDDING_DIM - 1)
    embedding_transport(lambda request: httpx.Response(200, json=embedding_body(wrong)))

    with pytest.raises(EmbeddingUnavailableError) as excinfo:
        run(embed_text("hello"))

    message = str(excinfo.value)
    assert f"expected {embeddings.EMBEDDING_DIM}" in message
    assert f"got {len(wrong)}" in message
    assert embeddings.EMBEDDING_MODEL in message


def test_a_wrong_length_embedding_degrades_the_request(degraded_route, embedding_transport,
                                                       monkeypatch):
    """End to end through the real embed_text(): a 200 answered as a degraded miss."""
    wrong = [0.1] * (embeddings.EMBEDDING_DIM + 8)
    embedding_transport(lambda request: httpx.Response(200, json=embedding_body(wrong)))
    monkeypatch.setattr(proxy_module, "embed_text", embed_text)

    response = run(proxy_module.proxy(FakeRequest(chat_body("What is pgvector?"))))

    assert response.status_code == 200
    assert response.headers["X-Cache"] == "MISS"
    assert response.headers["X-Cache-Degraded"] == "embedding-unavailable"
    assert degraded_route["semantic_lookups"] == 0
    assert degraded_route["semantic_writes"] == []
