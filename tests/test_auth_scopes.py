"""End-to-end auth tests for the two scoped credentials.

These drive the real app through TestClient rather than calling handlers directly,
because the thing under test is the wiring — dependency, scope gate, and the fact
that no route reads a tenant identifier from the caller.

Only the tenant *lookup* is faked (app.auth.get_tenant_by_token), so the hashing
and scope-selection logic in app.security still runs for real.
"""

from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

import app.auth as auth
import app.db as db
import app.routes.proxy as proxy_module
from app.main import app
from app.security import SCOPE_DASHBOARD, SCOPE_PROXY, hash_token

TENANT_A = {"id": 1, "uuid": uuid4(), "name": "Tenant A"}
TENANT_B = {"id": 2, "uuid": uuid4(), "name": "Tenant B"}

A_PROXY_KEY = "sk-tenant-a-proxy"
A_DASHBOARD_TOKEN = "dash_tenant-a-dashboard"
B_PROXY_KEY = "sk-tenant-b-proxy"
B_DASHBOARD_TOKEN = "dash_tenant-b-dashboard"

# Keyed by (scope, hash) exactly as the real tenants table is: one hash column per
# scope, and only hashes stored.
TOKEN_TABLE = {
    (SCOPE_PROXY, hash_token(A_PROXY_KEY)): TENANT_A,
    (SCOPE_DASHBOARD, hash_token(A_DASHBOARD_TOKEN)): TENANT_A,
    (SCOPE_PROXY, hash_token(B_PROXY_KEY)): TENANT_B,
    (SCOPE_DASHBOARD, hash_token(B_DASHBOARD_TOKEN)): TENANT_B,
}

# Per-tenant rows with values that differ by an order of magnitude, so a row
# belonging to the wrong tenant is unmistakable rather than plausible.
def _stats_row(count):
    return {
        "cache_status": "EXACT_HIT",
        "count": count,
        "avg_latency_ms": 11.0,
        "prompt_tokens": count * 100,
        "completion_tokens": count * 10,
    }


STATS_ROWS = {TENANT_A["id"]: [_stats_row(7)], TENANT_B["id"]: [_stats_row(999)]}

# Distinct per tenant on purpose: the models list is part of the stats payload, so
# it has to be as tenant-scoped as the counts are.
MODEL_ROWS = {
    TENANT_A["id"]: [{"model": "mistral-small-latest", "count": 7}],
    TENANT_B["id"]: [{"model": "mistral-large-latest", "count": 999}],
}

TREND_ROWS = {
    TENANT_A["id"]: [{"day": "2026-08-10T00:00:00+00:00", "total_requests": 7, "hit_count": 7}],
    TENANT_B["id"]: [{"day": "2026-08-10T00:00:00+00:00", "total_requests": 999, "hit_count": 999}],
}

TOP_CACHED_ROWS = {
    str(TENANT_A["uuid"]): [
        {
            "normalized_query": "tenant a query",
            "hit_count": 7,
            "min_similarity": 0.95,
            "max_similarity": 0.99,
        }
    ],
    str(TENANT_B["uuid"]): [
        {
            "normalized_query": "tenant b secret query",
            "hit_count": 999,
            "min_similarity": 0.93,
            "max_similarity": 0.97,
        }
    ],
}


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


class TenantScopedConn:
    """Returns rows only for the tenant the query actually asked for.

    Dispatching on the SQL keeps one fake behind all three stats routes; the
    important property is that the tenant filter in the query is the *only* thing
    deciding which rows come back.
    """

    def __init__(self):
        self.executed = []

    async def fetch(self, sql, param):
        if "cache_entries" in sql:
            return TOP_CACHED_ROWS.get(param, [])
        if "date_trunc" in sql:
            return TREND_ROWS.get(param, [])
        if "GROUP BY COALESCE(model" in sql:
            return MODEL_ROWS.get(param, [])
        return STATS_ROWS.get(param, [])

    async def execute(self, sql, *args):
        self.executed.append((sql, args))


def bearer(token):
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture
def client(monkeypatch):
    async def fake_get_tenant_by_token(token, scope):
        return TOKEN_TABLE.get((scope, hash_token(token)))

    monkeypatch.setattr(auth, "get_tenant_by_token", fake_get_tenant_by_token)
    monkeypatch.setattr(db, "pool", FakePool(TenantScopedConn()))
    # TestClient without a `with` block does not run the lifespan, so the real
    # init_db_pool never fires and the fake pool above stays in place.
    return TestClient(app)


def test_dashboard_token_reads_only_its_own_tenants_stats(client):
    response = client.get("/v1/stats", headers=bearer(A_DASHBOARD_TOKEN))

    assert response.status_code == 200
    assert response.json()["tenant_id"] == TENANT_A["id"]
    assert response.json()["exact_hit_count"] == 7


def test_tenant_a_cannot_read_tenant_b_stats_via_query_param(client):
    """The old `?tenant_id=` escape hatch must be inert, not merely undocumented."""
    for path in ("/v1/stats", "/v1/stats/trend", "/v1/stats/top-cached"):
        response = client.get(
            f"{path}?tenant_id={TENANT_B['id']}", headers=bearer(A_DASHBOARD_TOKEN)
        )

        assert response.status_code == 200, path
        assert response.json()["tenant_id"] == TENANT_A["id"], path

    # Not just the echoed tenant_id — the payload itself must be A's.
    stats = client.get(
        f"/v1/stats?tenant_id={TENANT_B['id']}", headers=bearer(A_DASHBOARD_TOKEN)
    ).json()
    assert stats["exact_hit_count"] == 7

    top = client.get(
        f"/v1/stats/top-cached?tenant_id={TENANT_B['id']}", headers=bearer(A_DASHBOARD_TOKEN)
    ).json()
    assert [e["normalized_query"] for e in top["entries"]] == ["tenant a query"]


def test_each_token_resolves_to_its_own_tenant(client):
    a = client.get("/v1/stats", headers=bearer(A_DASHBOARD_TOKEN)).json()
    b = client.get("/v1/stats", headers=bearer(B_DASHBOARD_TOKEN)).json()

    assert a["tenant_id"] == TENANT_A["id"]
    assert b["tenant_id"] == TENANT_B["id"]
    assert b["exact_hit_count"] == 999


@pytest.mark.parametrize("path", ["/v1/stats", "/v1/stats/trend", "/v1/stats/top-cached"])
def test_stats_require_a_key(client, path):
    assert client.get(path).status_code == 401
    assert client.get(path, headers={"Authorization": "Basic abc"}).status_code == 401
    assert client.get(path, headers=bearer("")).status_code == 401
    assert client.get(path, headers=bearer("dash_not-a-real-token")).status_code == 401


@pytest.mark.parametrize("path", ["/v1/stats", "/v1/stats/trend", "/v1/stats/top-cached"])
def test_proxy_key_is_rejected_by_the_stats_endpoints(client, path):
    """A valid credential in the wrong scope is as good as no credential."""
    assert client.get(path, headers=bearer(A_PROXY_KEY)).status_code == 401


def test_dashboard_token_is_rejected_by_the_proxy_route(client, monkeypatch):
    monkeypatch.setattr(proxy_module, "MISTRAL_API_KEY", "test-upstream-key")

    async def fail(*args, **kwargs):
        raise AssertionError("upstream must not be called for an unauthorized request")

    monkeypatch.setattr(proxy_module, "call_upstream_llm", fail)

    response = client.post(
        "/v1/chat/completions",
        headers=bearer(A_DASHBOARD_TOKEN),
        json={"messages": [{"role": "user", "content": "hi"}], "temperature": 0},
    )

    assert response.status_code == 401


def test_lookup_hashes_the_token_and_picks_the_column_for_the_scope(monkeypatch):
    """app.db never sends a plaintext credential to Postgres."""
    seen = []

    class RecordingConn:
        async def fetchrow(self, sql, param):
            seen.append((sql, param))
            return None

    monkeypatch.setattr(db, "pool", FakePool(RecordingConn()))

    import asyncio

    asyncio.run(db.get_tenant_by_token(A_PROXY_KEY, SCOPE_PROXY))
    asyncio.run(db.get_tenant_by_token(A_DASHBOARD_TOKEN, SCOPE_DASHBOARD))

    proxy_sql, proxy_param = seen[0]
    dashboard_sql, dashboard_param = seen[1]

    assert "api_key_hash = $1" in proxy_sql
    assert "dashboard_token_hash = $1" in dashboard_sql

    assert proxy_param == hash_token(A_PROXY_KEY)
    assert dashboard_param == hash_token(A_DASHBOARD_TOKEN)
    assert A_PROXY_KEY not in proxy_param
    assert A_DASHBOARD_TOKEN not in dashboard_param


def test_unknown_scope_is_a_programming_error(monkeypatch):
    monkeypatch.setattr(db, "pool", FakePool(TenantScopedConn()))

    import asyncio

    with pytest.raises(ValueError):
        asyncio.run(db.get_tenant_by_token(A_PROXY_KEY, "admin"))
