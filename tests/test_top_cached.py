import asyncio

import app.routes.proxy as proxy_module


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
    monkeypatch.setattr(proxy_module.db, "pool", FakePool(FakeTopCachedConn(rows)))

    result = run(proxy_module.get_top_cached(tenant_id=1))
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
    monkeypatch.setattr(proxy_module.db, "pool", FakePool(FakeTopCachedConn(rows)))

    entries = run(proxy_module.get_top_cached(tenant_id=1))["entries"]

    assert entries[0]["hit_count"] == 0
    assert entries[0]["min_similarity"] is None
    assert entries[0]["max_similarity"] is None


def test_top_cached_for_brand_new_tenant_is_empty(monkeypatch):
    monkeypatch.setattr(proxy_module.db, "pool", FakePool(FakeTopCachedConn([])))

    result = run(proxy_module.get_top_cached(tenant_id=99))

    assert result["entries"] == []
    assert result["tenant_id"] == 99


def test_top_cached_joins_tenants_and_limits_to_20(monkeypatch):
    conn = FakeTopCachedConn([])
    monkeypatch.setattr(proxy_module.db, "pool", FakePool(conn))

    run(proxy_module.get_top_cached(tenant_id=3))

    sql, param = conn.calls[0]
    assert param == 3
    # cache_entries keys on tenants.uuid, so the integer tenant_id must be joined through.
    assert "JOIN tenants ON tenants.uuid = cache_entries.tenant_id" in sql
    assert "WHERE tenants.id = $1" in sql
    assert "ORDER BY cache_entries.hit_count DESC" in sql
    assert "LIMIT 20" in sql
