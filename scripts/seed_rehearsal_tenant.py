"""Create one empty tenant for a traffic rehearsal, and print its credentials.

    python scripts/seed_rehearsal_tenant.py [--name "Bitext Rehearsal"]

Unlike `scripts/seed_demo.py`, this seeds **no** cache entries and **no** request
history. That is the entire point: a rehearsal measures what real traffic does, so
every number the dashboard and /v1/stats* report afterwards has to have come from
requests that actually went through the proxy. Reusing the demo tenants would mix
their synthetic 7-day history into the result.

Credentials are freshly generated per run (`app/security.py`), never fixed like the
demo ones — this tenant can hold a real, billable proxy key, so there is nothing
safe about publishing it in a file. They are printed once, here, and only their
SHA-256 hashes are stored.

Reads DATABASE_URL from the environment, falling back to the compose Postgres on
port 5433 (see docker-compose.yml: the container's 5432 is published on 5433
because a native Postgres holds 5432 on this machine).

Re-running with the same --name creates a *new* tenant with new credentials; it
does not upsert, because there is no fixed key to conflict on. Use --reset to
clear an existing rehearsal tenant's data instead of accumulating runs.
"""

import argparse
import asyncio
import os
import pathlib
import sys

import asyncpg

REPO = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from app.security import (  # noqa: E402  (path setup must precede the import)
    generate_dashboard_token,
    generate_proxy_key,
    hash_token,
)
from scripts._migrations import (  # noqa: E402  (same)
    FIRST_MIGRATION,
    MIGRATIONS_DIR,
    apply_rerunnable_migrations,
)

DATABASE_URL = os.getenv(
    "DATABASE_URL", "postgresql://postgres:postgres@localhost:5433/semanticdb"
)


async def ensure_schema(conn: asyncpg.Connection) -> None:
    """Same contract as seed_demo.ensure_schema: 001 only on a fresh database."""
    if await conn.fetchval("SELECT to_regclass('public.tenants')") is None:
        await conn.execute((MIGRATIONS_DIR / FIRST_MIGRATION).read_text(encoding="utf-8"))
        print(f"  applied {FIRST_MIGRATION}")

    for name in await apply_rerunnable_migrations(conn):
        print(f"  applied {name}")

    # 001 creates a plaintext api_key column with NOT NULL on a fresh database, and
    # this tenant only ever populates the hash columns. Same guard as seed_demo.py.
    await conn.execute(
        """
        DO $$
        BEGIN
            IF EXISTS (
                SELECT 1 FROM information_schema.columns
                WHERE table_name = 'tenants' AND column_name = 'api_key'
            ) THEN
                ALTER TABLE tenants ALTER COLUMN api_key DROP NOT NULL;
            END IF;
        END $$;
        """
    )


async def insert_tenant(conn: asyncpg.Connection, name: str, proxy_key: str,
                        dashboard_token: str):
    """Insert one tenant, storing only hashes.

    Mirrors seed_demo.upsert_tenant's INSERT, including the restated predicate on
    the partial index, but without the ON CONFLICT arm: the credentials are random
    per run, so a conflict here would mean a SHA-256 collision, not a re-run.
    """
    return await conn.fetchrow(
        """
        INSERT INTO tenants (api_key_hash, dashboard_token_hash, name)
        VALUES ($1, $2, $3)
        RETURNING id, uuid
        """,
        hash_token(proxy_key),
        hash_token(dashboard_token),
        name,
    )


async def clear_tenant_data(conn: asyncpg.Connection, name: str) -> None:
    """Drop cache entries and request history for every tenant with this name."""
    tenants = await conn.fetch("SELECT id, uuid FROM tenants WHERE name = $1", name)
    for tenant in tenants:
        entries = await conn.execute(
            "DELETE FROM cache_entries WHERE tenant_id = $1::uuid", str(tenant["uuid"])
        )
        requests = await conn.execute(
            "DELETE FROM requests WHERE tenant_id = $1", tenant["id"]
        )
        print(f"  cleared tenant {tenant['id']} ({entries.lower()}, {requests.lower()})")


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--name", default="Bitext Rehearsal", help="tenant display name")
    parser.add_argument(
        "--reset",
        action="store_true",
        help="delete cache entries and request history for existing tenants of this name",
    )
    args = parser.parse_args()

    print(f"Connecting to {DATABASE_URL.rsplit('@', 1)[-1]}")
    try:
        conn = await asyncpg.connect(DATABASE_URL)
    except Exception as exc:
        print(f"\nCould not connect: {type(exc).__name__}: {exc}")
        print("Is the compose Postgres up? `docker compose up -d postgres`")
        return 1

    try:
        await ensure_schema(conn)

        if args.reset:
            await clear_tenant_data(conn, args.name)

        proxy_key = generate_proxy_key()
        dashboard_token = generate_dashboard_token()
        tenant = await insert_tenant(conn, args.name, proxy_key, dashboard_token)

        others = await conn.fetch(
            "SELECT name, count(*) AS n FROM tenants GROUP BY name ORDER BY name"
        )

        print(f"\nCreated tenant {tenant['id']}  \"{args.name}\"")
        print(f"  uuid            {tenant['uuid']}")
        print(f"  proxy key       {proxy_key}")
        print(f"  dashboard token {dashboard_token}")
        print("\nShown once. Only SHA-256 hashes are stored, so these cannot be recovered.")
        print("\nTenants now in this database:")
        for row in others:
            print(f"  {row['n']}x {row['name']}")
        print(
            f"\nPoint the dashboard at it with, in dashboard/.env.local:"
            f"\n  GATEWAY_DASHBOARD_TOKEN={dashboard_token}"
        )
    finally:
        await conn.close()

    return 0


sys.exit(asyncio.run(main()))
