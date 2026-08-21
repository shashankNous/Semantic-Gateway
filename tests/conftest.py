import pytest

from app.services import rate_limit


class InMemoryRedis:
    """Working in-memory stand-in for the counter commands the limiter uses.

    Every test gets one of these by default, for two reasons: no test opens a
    socket to the configured REDIS_URL (which turned a 2-second suite into a
    22-second one waiting on connection timeouts), and the limiter exercises its
    real INCR/EXPIRE/TTL path rather than its error path.

    It must *work* rather than raise. With RATE_LIMIT_FAIL_MODE defaulting to
    `closed`, a fake that raised would turn every proxy-route test into a 503.
    Tests that want to exercise the outage path install their own failing fake.
    """

    def __init__(self):
        self.counts = {}
        self.ttls = {}

    async def incr(self, key):
        self.counts[key] = self.counts.get(key, 0) + 1
        self.ttls.setdefault(key, -1)
        return self.counts[key]

    async def expire(self, key, seconds):
        self.ttls[key] = seconds
        return True

    async def ttl(self, key):
        return self.ttls.get(key, -2)


@pytest.fixture(autouse=True)
def _no_real_redis(monkeypatch):
    monkeypatch.setattr(rate_limit, "redis_client", InMemoryRedis())
