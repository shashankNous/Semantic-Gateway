import asyncio
from uuid import uuid4

import app.routes.stats as stats_module


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


class FakeTopCachedConn:
    def __init__(self, rows):
        self.rows = rows
        self.calls = []

    async def fetch(self, *args):
        self.calls.append(args)
        return self.rows


def dashboard_tenant(tenant_id, tenant_uuid=None):
    """What app.auth.require_dashboard_key() hands the route once the token resolves.

    The routes take this dict, never a caller-supplied tenant_id — that is the
    whole point of Task 2, so the tests call them the same way the app does.
    """
    return {
        "tenant_id": tenant_id,
        "tenant_uuid": tenant_uuid or uuid4(),
        "name": f"tenant-{tenant_id}",
        "scope": "dashboard",
    }


def run(coro):
    return asyncio.run(coro)


def entry(query, hit_count, min_similarity=None, max_similarity=None):
    return {
        "normalized_query": query,
        "hit_count": hit_count,
        "min_similarity": min_similarity,
        "max_similarity": max_similarity,
    }


def test_top_cached_returns_entries_with_similarity_range(monkeypatch):
    rows = [
        entry("what is the refund policy", 12, 0.921, 0.998),
        entry("how do i reset my password", 5, 0.94, 0.94),
    ]
    monkeypatch.setattr(stats_module.db, "pool", FakePool(FakeTopCachedConn(rows)))

    result = run(stats_module.get_top_cached(tenant=dashboard_tenant(1)))
    entries = result["entries"]

    assert result["tenant_id"] == 1
    assert len(entries) == 2
    assert entries[0] == {
        "normalized_query": "what is the refund policy",
        "hit_count": 12,
        "min_similarity": 0.921,
        "max_similarity": 0.998,
    }
    assert entries[1]["min_similarity"] == entries[1]["max_similarity"] == 0.94


def test_top_cached_handles_never_hit_entries(monkeypatch):
    # An entry that has never been served has NULL similarity bounds and, for rows
    # predating migration 004, a NULL hit_count.
    rows = [entry("a query nobody repeated", None)]
    monkeypatch.setattr(stats_module.db, "pool", FakePool(FakeTopCachedConn(rows)))

    entries = run(stats_module.get_top_cached(tenant=dashboard_tenant(1)))["entries"]

    assert entries[0]["hit_count"] == 0
    assert entries[0]["min_similarity"] is None
    assert entries[0]["max_similarity"] is None


def test_top_cached_for_brand_new_tenant_is_empty(monkeypatch):
    monkeypatch.setattr(stats_module.db, "pool", FakePool(FakeTopCachedConn([])))

    result = run(stats_module.get_top_cached(tenant=dashboard_tenant(99)))

    assert result["entries"] == []
    assert result["tenant_id"] == 99


def test_top_cached_scopes_to_the_tokens_tenant_uuid_and_limits_to_20(monkeypatch):
    conn = FakeTopCachedConn([])
    monkeypatch.setattr(stats_module.db, "pool", FakePool(conn))

    tenant_uuid = uuid4()
    run(stats_module.get_top_cached(tenant=dashboard_tenant(3, tenant_uuid)))

    sql, param = conn.calls[0]
    # cache_entries keys on tenants.uuid, which the token lookup already resolved,
    # so this filters on it directly rather than joining back through tenants.
    assert param == str(tenant_uuid)
    assert "WHERE tenant_id = $1::uuid" in sql
    assert "JOIN" not in sql
    assert "ORDER BY hit_count DESC" in sql
    assert "LIMIT 20" in sql
