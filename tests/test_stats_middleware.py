"""CORS lock and access logging on the /v1/stats* surface."""

from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

import app.auth as auth
import app.db as db
import app.middleware as middleware
from app.main import app
from app.security import SCOPE_DASHBOARD, hash_token

TENANT = {"id": 1, "uuid": uuid4(), "name": "Tenant A"}
DASHBOARD_TOKEN = "dash_tenant-a-dashboard"

ALLOWED_ORIGIN = "https://dashboard.vercel.app"
OTHER_ORIGIN = "https://attacker.example"


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


class RecordingConn:
    def __init__(self):
        self.inserts = []

    async def fetch(self, sql, param):
        return []

    async def execute(self, sql, *args):
        if "stats_access_log" in sql:
            self.inserts.append(args)


@pytest.fixture
def conn(monkeypatch):
    async def fake_get_tenant_by_token(token, scope):
        if scope == SCOPE_DASHBOARD and hash_token(token) == hash_token(DASHBOARD_TOKEN):
            return TENANT
        return None

    recording = RecordingConn()
    monkeypatch.setattr(auth, "get_tenant_by_token", fake_get_tenant_by_token)
    monkeypatch.setattr(db, "pool", FakePool(recording))
    monkeypatch.setenv("DASHBOARD_ORIGIN", ALLOWED_ORIGIN)
    return recording


@pytest.fixture
def client(conn):
    return TestClient(app)


def bearer(token=DASHBOARD_TOKEN):
    return {"Authorization": f"Bearer {token}"}


# --- CORS ---------------------------------------------------------------------


def test_dashboard_origin_is_allowed(client):
    response = client.get("/v1/stats", headers={**bearer(), "Origin": ALLOWED_ORIGIN})

    assert response.status_code == 200
    assert response.headers["Access-Control-Allow-Origin"] == ALLOWED_ORIGIN
    assert response.headers["Vary"] == "Origin"


def test_other_origins_get_no_allow_header(client):
    response = client.get("/v1/stats", headers={**bearer(), "Origin": OTHER_ORIGIN})

    # The request itself still runs — CORS is enforced by the browser, and the
    # absence of this header is what stops the page reading the body.
    assert "Access-Control-Allow-Origin" not in response.headers


def test_preflight_is_answered_for_the_dashboard_origin(client):
    response = client.options(
        "/v1/stats",
        headers={
            "Origin": ALLOWED_ORIGIN,
            "Access-Control-Request-Method": "GET",
            "Access-Control-Request-Headers": "Authorization",
        },
    )

    assert response.status_code == 204
    assert response.headers["Access-Control-Allow-Origin"] == ALLOWED_ORIGIN
    assert "Authorization" in response.headers["Access-Control-Allow-Headers"]


def test_preflight_from_another_origin_is_refused(client):
    response = client.options(
        "/v1/stats",
        headers={"Origin": OTHER_ORIGIN, "Access-Control-Request-Method": "GET"},
    )

    assert response.status_code == 403
    assert "Access-Control-Allow-Origin" not in response.headers


def test_no_origin_configured_allows_nothing(client, monkeypatch):
    monkeypatch.delenv("DASHBOARD_ORIGIN", raising=False)

    response = client.get("/v1/stats", headers={**bearer(), "Origin": ALLOWED_ORIGIN})

    assert "Access-Control-Allow-Origin" not in response.headers


def test_multiple_origins_can_be_configured(client, monkeypatch):
    monkeypatch.setenv("DASHBOARD_ORIGIN", f"{OTHER_ORIGIN}, {ALLOWED_ORIGIN}")

    for origin in (OTHER_ORIGIN, ALLOWED_ORIGIN):
        response = client.get("/v1/stats", headers={**bearer(), "Origin": origin})
        assert response.headers["Access-Control-Allow-Origin"] == origin


def test_proxy_route_cors_policy_is_untouched(client):
    """The stats CORS middleware must not start answering for /v1/chat/completions."""
    response = client.post(
        "/v1/chat/completions",
        headers={"Origin": ALLOWED_ORIGIN},
        json={"messages": []},
    )

    assert "Access-Control-Allow-Origin" not in response.headers


# --- Access log ---------------------------------------------------------------


def test_successful_stats_call_is_logged(client, conn):
    client.get("/v1/stats", headers=bearer())

    assert len(conn.inserts) == 1
    tenant_id, endpoint, requester_ip, status_code = conn.inserts[0]
    assert tenant_id == TENANT["id"]
    assert endpoint == "/v1/stats"
    assert status_code == 200
    assert requester_ip is not None


def test_rejected_stats_call_is_logged_without_a_tenant(client, conn):
    client.get("/v1/stats/trend", headers=bearer("dash_wrong-token"))

    tenant_id, endpoint, _ip, status_code = conn.inserts[0]
    assert tenant_id is None
    assert endpoint == "/v1/stats/trend"
    assert status_code == 401


def test_forwarded_ip_is_preferred_over_the_socket_peer(client, conn):
    client.get(
        "/v1/stats",
        headers={**bearer(), "X-Forwarded-For": "203.0.113.9, 10.0.0.1"},
    )

    assert conn.inserts[0][2] == "203.0.113.9"


def test_non_stats_routes_are_not_logged(client, conn):
    client.get("/health")

    assert conn.inserts == []


def test_a_failing_log_write_does_not_fail_the_request(client, conn, monkeypatch):
    async def boom(sql, *args):
        raise ConnectionError("postgres is down")

    monkeypatch.setattr(conn, "execute", boom)

    assert client.get("/v1/stats", headers=bearer()).status_code == 200


def test_stats_path_matching_does_not_catch_lookalike_routes():
    assert middleware.is_stats_path("/v1/stats")
    assert middleware.is_stats_path("/v1/stats/trend")
    assert not middleware.is_stats_path("/v1/statsomething")
    assert not middleware.is_stats_path("/v1/chat/completions")
