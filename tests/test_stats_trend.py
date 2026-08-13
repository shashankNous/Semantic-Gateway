import asyncio
from datetime import datetime, timezone

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


class FakeTrendConn:
    def __init__(self, rows):
        self.rows = rows
        self.calls = []

    async def fetch(self, *args):
        self.calls.append(args)
        return self.rows


def run(coro):
    return asyncio.run(coro)


def day(year, month, d):
    return datetime(year, month, d, tzinfo=timezone.utc)


def test_trend_returns_daily_hit_rate_oldest_first(monkeypatch):
    rows = [
        {"day": day(2026, 8, 10), "total_requests": 10, "hit_count": 4},
        {"day": day(2026, 8, 11), "total_requests": 8, "hit_count": 6},
        {"day": day(2026, 8, 12), "total_requests": 3, "hit_count": 3},
    ]
    monkeypatch.setattr(proxy_module.db, "pool", FakePool(FakeTrendConn(rows)))

    result = run(proxy_module.get_stats_trend(tenant_id=1))
    trend = result["trend"]

    assert result["tenant_id"] == 1
    assert [point["date"] for point in trend] == ["2026-08-10", "2026-08-11", "2026-08-12"]
    assert trend[0] == {
        "date": "2026-08-10",
        "total_requests": 10,
        "hit_count": 4,
        "hit_rate": 0.4,
    }
    assert trend[1]["hit_rate"] == 0.75
    assert trend[2]["hit_rate"] == 1.0


def test_trend_handles_day_with_no_requests(monkeypatch):
    rows = [{"day": day(2026, 8, 12), "total_requests": 0, "hit_count": 0}]
    monkeypatch.setattr(proxy_module.db, "pool", FakePool(FakeTrendConn(rows)))

    trend = run(proxy_module.get_stats_trend(tenant_id=1))["trend"]

    assert trend[0]["hit_rate"] == 0.0


def test_trend_accepts_string_and_date_days(monkeypatch):
    rows = [
        {"day": "2026-08-10T00:00:00+00:00", "total_requests": 2, "hit_count": 1},
        {"day": day(2026, 8, 11).date(), "total_requests": 2, "hit_count": 1},
    ]
    monkeypatch.setattr(proxy_module.db, "pool", FakePool(FakeTrendConn(rows)))

    trend = run(proxy_module.get_stats_trend(tenant_id=1))["trend"]

    assert [point["date"] for point in trend] == ["2026-08-10", "2026-08-11"]


def test_trend_for_brand_new_tenant_is_empty(monkeypatch):
    monkeypatch.setattr(proxy_module.db, "pool", FakePool(FakeTrendConn([])))

    result = run(proxy_module.get_stats_trend(tenant_id=99))

    assert result["trend"] == []
    assert result["tenant_id"] == 99


def test_trend_query_is_scoped_to_tenant_and_last_7_days(monkeypatch):
    conn = FakeTrendConn([])
    monkeypatch.setattr(proxy_module.db, "pool", FakePool(conn))

    run(proxy_module.get_stats_trend(tenant_id=7))

    sql, param = conn.calls[0]
    assert param == 7
    assert "tenant_id = $1" in sql
    assert "INTERVAL '6 days'" in sql
    assert "date_trunc('day', timestamp)" in sql
