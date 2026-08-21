"""Per-tenant request rate limiting, backed by the Redis instance the exact-match
cache already uses.

Fixed window: `INCR tenant:{id}:count`, then `EXPIRE RATE_LIMIT_WINDOW_SECONDS`
on the first increment of a window. A fixed window can let a tenant land up to 2x
the limit across a window boundary, which is an acceptable trade for a
partner-facing throttle — it costs one round trip and needs no sorted sets to
maintain.

The window length is `RATE_LIMIT_WINDOW_SECONDS` (default 60), so `limit` and
`window` are tuned independently. `RATE_LIMIT_PER_MINUTE` keeps its name for
backwards compatibility with deployments already setting it; read it as "requests
per window", which is only literally per-minute at the default window.

When Redis is unreachable there is no safe answer, only a choice of which failure
you would rather have. `RATE_LIMIT_FAIL_MODE` makes that choice explicit:

  * `closed` (default) — reject with 503. The cap holds during the outage, at the
    cost of the gateway being down for everyone while Redis is.
  * `open` — serve the request uncapped. The gateway stays up, but every tenant is
    unthrottled for the duration, which is when abuse is cheapest to attempt.

Default is `closed` because an unthrottled window is the more expensive failure:
misses go upstream and cost real money per request, and there is no ceiling on it.
A 503 is loud, bounded, and recovers by itself when Redis returns. Set `open` for
availability-first deployments where the upstream spend is capped some other way.
"""

import logging
import os

from fastapi import HTTPException

from app.services.cache import redis_client

logger = logging.getLogger(__name__)

#: Requests per tenant per window. Tunable per partner without a redeploy;
#: set to 0 (or below) to disable throttling entirely.
RATE_LIMIT_PER_MINUTE = int(os.getenv("RATE_LIMIT_PER_MINUTE", "100"))

#: Length of that window, in seconds. Also the `Retry-After` value on both the 429
#: and the fail-closed 503, so a caller that honours it retries into a fresh window.
RATE_LIMIT_WINDOW_SECONDS = int(os.getenv("RATE_LIMIT_WINDOW_SECONDS", "60"))

FAIL_OPEN = "open"
FAIL_CLOSED = "closed"

#: What to do when Redis cannot be reached. See the module docstring.
RATE_LIMIT_FAIL_MODE = os.getenv("RATE_LIMIT_FAIL_MODE", FAIL_CLOSED).strip().lower()

if RATE_LIMIT_FAIL_MODE not in (FAIL_OPEN, FAIL_CLOSED):
    # Refuse to guess. Silently coercing a typo like "close" to a default would
    # pick a security posture the operator did not ask for.
    raise RuntimeError(
        f"RATE_LIMIT_FAIL_MODE must be {FAIL_OPEN!r} or {FAIL_CLOSED!r}, "
        f"got {RATE_LIMIT_FAIL_MODE!r}"
    )


def _key(tenant_id) -> str:
    return f"tenant:{tenant_id}:count"


async def enforce_rate_limit(tenant_id, limit: int | None = None) -> int:
    """Count one request for `tenant_id`; raise 429 once over the limit.

    Returns the request count within the current window. Raises 503 instead if
    Redis is unreachable and the fail mode is `closed`.
    """
    limit = RATE_LIMIT_PER_MINUTE if limit is None else limit

    if limit <= 0:
        return 0

    key = _key(tenant_id)

    try:
        count = await redis_client.incr(key)

        if count == 1:
            await redis_client.expire(key, RATE_LIMIT_WINDOW_SECONDS)
        else:
            # A key that lost its TTL would throttle the tenant forever once it
            # crossed the limit, so re-arm the window rather than trust INCR==1.
            if await redis_client.ttl(key) < 0:
                await redis_client.expire(key, RATE_LIMIT_WINDOW_SECONDS)
    except Exception:
        # Read the mode at call time, not import time, so it can be flipped in a
        # test or a REPL without reimporting the module.
        if RATE_LIMIT_FAIL_MODE == FAIL_OPEN:
            logger.exception(
                "Rate limit check failed for tenant %s; allowing request "
                "(RATE_LIMIT_FAIL_MODE=open)",
                tenant_id,
            )
            return 0

        logger.exception(
            "Rate limit check failed for tenant %s; rejecting request "
            "(RATE_LIMIT_FAIL_MODE=closed)",
            tenant_id,
        )
        raise HTTPException(
            status_code=503,
            detail="Rate limiting is unavailable; request rejected",
            headers={"Retry-After": str(RATE_LIMIT_WINDOW_SECONDS)},
        ) from None

    if count > limit:
        raise HTTPException(
            status_code=429,
            detail=f"Rate limit exceeded: {limit} requests per {RATE_LIMIT_WINDOW_SECONDS}s",
            headers={"Retry-After": str(RATE_LIMIT_WINDOW_SECONDS)},
        )

    return count
