"""Bearer-token authentication, scoped per credential type.

One trust boundary, two doors into it:

  * `verify_api_key`        — proxy scope (`sk-...`), guards /v1/chat/completions
  * `require_dashboard_key` — dashboard scope (`dash_...`), guards /v1/stats*

Both resolve the tenant **server-side from the token**. No route takes a tenant
identifier from the caller, so there is no cross-tenant read to attempt.
"""

from fastapi import Request, HTTPException

from app.db import get_tenant_by_token
from app.security import SCOPE_DASHBOARD, SCOPE_PROXY


def _bearer_token(request: Request) -> str:
    auth_header = request.headers.get("Authorization", "")

    if not auth_header.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Missing or invalid Authorization header")

    token = auth_header.removeprefix("Bearer ").strip()

    if not token:
        raise HTTPException(status_code=401, detail="Missing or invalid Authorization header")

    return token


async def authenticate(request: Request, scope: str) -> dict:
    token = _bearer_token(request)
    tenant = await get_tenant_by_token(token, scope)

    if tenant is None:
        # A valid token presented to the wrong scope lands here too. Saying only
        # "unknown" avoids confirming to a holder of a leaked dashboard token that
        # it is a real credential for some other route.
        raise HTTPException(status_code=401, detail="Unknown or out-of-scope API key")

    resolved = {
        "tenant_id": tenant["id"],
        "tenant_uuid": tenant["uuid"],
        "name": tenant["name"],
        "scope": scope,
    }

    # Picked up by the stats access-log middleware, which runs after the route and
    # otherwise has no way to know who the caller turned out to be.
    request.state.tenant_id = resolved["tenant_id"]

    return resolved


async def verify_api_key(request: Request) -> dict:
    """Proxy-scoped auth. A dashboard token is rejected here."""
    return await authenticate(request, SCOPE_PROXY)


async def require_dashboard_key(request: Request) -> dict:
    """Dashboard-scoped auth, as a FastAPI dependency for the /v1/stats* routes.

    A proxy key is rejected here: the read-only surface deliberately does not
    accept the credential that can spend money.
    """
    return await authenticate(request, SCOPE_DASHBOARD)
