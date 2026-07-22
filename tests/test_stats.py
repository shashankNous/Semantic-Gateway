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


class FakeStatsConn:
    def __init__(self, rows):
        self.rows = rows

    async def fetch(self, *args):
        return self.rows


def run(coro):
    return asyncio.run(coro)


def test_get_stats_aggregates_by_cache_status(monkeypatch):
    rows = [
        {"cache_status": "EXACT_HIT", "count": 5, "avg_latency_ms": 10.0, "prompt_tokens": 100, "completion_tokens": 50},
        {"cache_status": "SEMANTIC_HIT", "count": 3, "avg_latency_ms": 20.0, "prompt_tokens": 60, "completion_tokens": 30},
        {"cache_status": "MISS", "count": 2, "avg_latency_ms": 500.0, "prompt_tokens": 40, "completion_tokens": 20},
        {"cache_status": "UNCACHEABLE", "count": 1, "avg_latency_ms": 300.0, "prompt_tokens": 10, "completion_tokens": 5},
    ]
    monkeypatch.setattr(proxy_module.db, "pool", FakePool(FakeStatsConn(rows)))

    stats = run(proxy_module.get_stats(tenant_id=1))

    assert stats["total_requests"] == 11
    assert stats["exact_hit_count"] == 5
    assert stats["semantic_hit_count"] == 3
    assert stats["miss_count"] == 2
    assert stats["uncacheable_count"] == 1
    assert stats["hit_rate"] == round(8 / 11, 4)
    assert stats["avg_latency_ms_by_cache_status"]["EXACT_HIT"] == 10.0
    assert stats["estimated_tokens_saved"] == 100 + 50 + 60 + 30


def test_get_stats_with_no_requests(monkeypatch):
    monkeypatch.setattr(proxy_module.db, "pool", FakePool(FakeStatsConn([])))

    stats = run(proxy_module.get_stats(tenant_id=42))

    assert stats["total_requests"] == 0
    assert stats["hit_rate"] == 0.0
    assert stats["estimated_tokens_saved"] == 0
