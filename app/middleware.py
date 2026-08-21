"""Middleware scoped to the `/v1/stats*` surface: CORS and an access log.

Both are deliberately path-scoped rather than app-wide. Starlette's stock
`CORSMiddleware` applies to every route, and the proxy route's CORS posture is a
separate decision from the dashboard's — see README for why the proxy stays with
no CORS headers at all.
"""

import logging
import os

from dotenv import load_dotenv
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import Response

import app.db as db

load_dotenv()

logger = logging.getLogger(__name__)

STATS_PATH_PREFIX = "/v1/stats"


def _configured_origins() -> list[str]:
    """Allowed browser origins for /v1/stats*, from DASHBOARD_ORIGIN.

    Comma-separated so a single deployment can serve a preview and a production
    dashboard. Unset means no origin is allowed, which is the safe default: the
    dashboard proxies through its own Next server today and sends no Origin at
    all, so nothing breaks by locking this down.
    """
    raw = os.getenv("DASHBOARD_ORIGIN", "")
    return [origin.strip().rstrip("/") for origin in raw.split(",") if origin.strip()]


def is_stats_path(path: str) -> bool:
    return path == STATS_PATH_PREFIX or path.startswith(f"{STATS_PATH_PREFIX}/")


def client_ip(request) -> str | None:
    """Caller IP, honouring the proxy chain Railway puts in front of the app.

    X-Forwarded-For is client-spoofable in general; behind Railway's edge the
    left-most entry is the real client. Good enough for an audit trail, not
    something to make an authorization decision on.
    """
    forwarded = request.headers.get("X-Forwarded-For")
    if forwarded:
        return forwarded.split(",")[0].strip() or None

    real_ip = request.headers.get("X-Real-IP")
    if real_ip:
        return real_ip.strip() or None

    return request.client.host if request.client else None


class StatsCORSMiddleware(BaseHTTPMiddleware):
    """Allow the dashboard origin — and only it — to read /v1/stats* from a browser."""

    async def dispatch(self, request, call_next):
        if not is_stats_path(request.url.path):
            return await call_next(request)

        origin = (request.headers.get("Origin") or "").rstrip("/")
        allowed = origin and origin in _configured_origins()

        if request.method == "OPTIONS" and "Access-Control-Request-Method" in request.headers:
            # Answer the preflight here; the routes are GET-only and would 405 it.
            response = Response(status_code=204 if allowed else 403)
        else:
            response = await call_next(request)

        if allowed:
            response.headers["Access-Control-Allow-Origin"] = origin
            response.headers["Access-Control-Allow-Methods"] = "GET, OPTIONS"
            response.headers["Access-Control-Allow-Headers"] = "Authorization, Content-Type"
            response.headers["Access-Control-Max-Age"] = "600"

            # The allowed origin varies by request, so caches must key on Origin.
            # Append rather than assign, so an existing Vary is not discarded.
            existing_vary = response.headers.get("Vary")
            if not existing_vary:
                response.headers["Vary"] = "Origin"
            elif "origin" not in existing_vary.lower():
                response.headers["Vary"] = f"{existing_vary}, Origin"

        return response


class StatsAccessLogMiddleware(BaseHTTPMiddleware):
    """Record every /v1/stats* call — including the rejected ones."""

    async def dispatch(self, request, call_next):
        if not is_stats_path(request.url.path):
            return await call_next(request)

        response = await call_next(request)

        # Set by app.auth.authenticate once the token resolves. Absent on a 401,
        # which is exactly the case worth recording without a tenant.
        tenant_id = getattr(request.state, "tenant_id", None)

        await db.log_stats_access(
            tenant_id=tenant_id,
            endpoint=request.url.path,
            requester_ip=client_ip(request),
            status_code=response.status_code,
        )

        return response
