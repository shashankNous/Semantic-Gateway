"""Token generation and hashing shared by the app and the migration scripts.

Two kinds of credential exist per tenant, and they are deliberately different
things rather than one key with two uses:

  * a **proxy key** (`sk-...`) — spends money, since a miss calls the upstream LLM
  * a **dashboard token** (`dash_...`) — read-only over `/v1/stats*`

The dashboard token is the one that ends up in a browser-reachable deployment, so
keeping it separate means a leaked dashboard token cannot run up an LLM bill.

Only the SHA-256 hash of either is ever stored. Lookups hash the presented token
and compare hashes, so the database never holds a usable credential.
"""

import hashlib
import secrets

PROXY_KEY_PREFIX = "sk-"
DASHBOARD_TOKEN_PREFIX = "dash_"

#: Scope names, which double as the logical column selector in app/db.py.
SCOPE_PROXY = "proxy"
SCOPE_DASHBOARD = "dashboard"


def hash_token(token: str) -> str:
    """SHA-256 hex digest of a token, as stored in `tenants`.

    Plain SHA-256 rather than a slow KDF on purpose: these are 192-bit random
    tokens, not user-chosen passwords, so there is no dictionary to attack and
    the per-request cost of bcrypt/argon2 would buy nothing. If tokens ever
    become user-supplied, this is the function to upgrade.
    """
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def generate_proxy_key() -> str:
    return f"{PROXY_KEY_PREFIX}{secrets.token_urlsafe(32)}"


def generate_dashboard_token() -> str:
    return f"{DASHBOARD_TOKEN_PREFIX}{secrets.token_urlsafe(32)}"
