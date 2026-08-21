"""One-time migration: hash tenant credentials and issue dashboard tokens.

    python scripts/migrate_hash_keys.py --mode=in-place
    python scripts/migrate_hash_keys.py --mode=rotate
    python scripts/migrate_hash_keys.py --mode=in-place --dry-run

`--mode` is **required**. The two modes differ in whether every existing caller
keeps working or is broken until re-keyed, and the plaintext column is dropped
afterwards either way — so the choice cannot be undone by re-running. A default
would make that decision silently.

  * `--mode=in-place` — hash the existing plaintext keys. Callers keep working,
    because the plaintext is still readable at migration time. Choose this for a
    routine hardening pass.
  * `--mode=rotate` — mint fresh `sk-...` keys and hash those instead. **Breaking:**
    every existing key stops working the moment this runs. Choose this if you must
    assume the old keys leaked.

`--dry-run` is independent and works with either mode: schema migrations still
apply, but no credential is written and nothing is dropped.

What it does, in order:

1. Applies every re-runnable migration in `migrations/` (`002` and up) in filename
   order. They are all idempotent, so this is a no-op on an already-migrated
   database, and a newly added `007_*.sql` is picked up with no change here — see
   `scripts/_migrations.py`. `001_migrations.sql` is *not* applied: this script
   needs the `tenants` table to already exist and refuses to run without it.
2. Backfills `tenants.api_key_hash`, per the chosen mode.
3. Issues a `dash_...` dashboard token for every tenant that lacks one.
4. Drops the plaintext `tenants.api_key` column, once every row has a hash.

Newly issued tokens are printed **once**. A hash cannot be reversed, so a token
not captured from this output is gone and the tenant has to be re-issued one.

Reads DATABASE_URL from the environment. Point it at one database at a time and
read the summary before trusting it against production.
"""

import asyncio
import os
import pathlib
import sys
from typing import NamedTuple

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
    apply_rerunnable_migrations,
)

DATABASE_URL = os.getenv(
    "DATABASE_URL", "postgresql://postgres:demopw@localhost:5433/semanticdb"
)


#: Stands in for a credential value on a dry run, which generates none.
PENDING = "(issued by the real run)"


class Issued(NamedTuple):
    """What one tenant ends up with, for the credential block printed at the end.

    `proxy_key` / `dashboard_token` carry the real value on a live run and None on
    a dry run, which generates no token at all. The `would_issue_*` flags are what a
    dry run reports from, so it can say *that* a credential is coming without
    printing one that will never exist.
    """

    tenant_id: int
    name: str
    proxy_key: str | None
    dashboard_token: str | None
    would_issue_proxy_key: bool
    would_issue_dashboard_token: bool


async def has_column(conn: asyncpg.Connection, table: str, column: str) -> bool:
    return bool(
        await conn.fetchval(
            """
            SELECT 1 FROM information_schema.columns
            WHERE table_name = $1 AND column_name = $2
            """,
            table,
            column,
        )
    )


async def ensure_schema(conn: asyncpg.Connection) -> None:
    if await conn.fetchval("SELECT to_regclass('public.tenants')") is None:
        raise RuntimeError(
            f"No `tenants` table in this database. Apply migrations/{FIRST_MIGRATION} "
            "first (see README), or run scripts/seed_demo.py for a local demo database."
        )

    # Everything except 001, globbed rather than named, so a future migration is not
    # silently skipped here. All of these are idempotent; re-applying costs nothing.
    for name in await apply_rerunnable_migrations(conn):
        print(f"  applied {name}")


async def migrate(conn: asyncpg.Connection, rotate: bool, dry_run: bool) -> int:
    plaintext_present = await has_column(conn, "tenants", "api_key")

    columns = "id, name, api_key_hash, dashboard_token_hash"
    if plaintext_present:
        columns += ", api_key"

    tenants = await conn.fetch(f"SELECT {columns} FROM tenants ORDER BY id")

    if not tenants:
        print("\nNo tenants to migrate.")
        return 0

    issued: list[Issued] = []
    # Only tenants whose *existing* key was rehashed from the plaintext they still
    # hold. A tenant that got a freshly minted key must not be reported alongside
    # these: it has no old key, so "unchanged and keeps working" would be false.
    rehashed_in_place: list[int] = []

    for tenant in tenants:
        new_proxy_key: str | None = None
        new_dashboard_token: str | None = None
        needs_new_proxy_key = False

        needs_proxy_hash = tenant["api_key_hash"] is None
        if rotate or needs_proxy_hash:
            # Rehashing the key the tenant already holds is the one branch that
            # leaves callers working; every other one mints a key they do not have.
            rehash_existing = not rotate and plaintext_present and bool(tenant["api_key"])
            needs_new_proxy_key = not rehash_existing

            if rehash_existing:
                rehashed_in_place.append(tenant["id"])

            # Generation sits inside the write guard on purpose: a token minted on a
            # dry run is discarded, and printing one is how an operator ends up
            # storing a credential the database never received.
            if not dry_run:
                if rehash_existing:
                    proxy_hash = hash_token(tenant["api_key"])
                else:
                    # No plaintext left to rehash and no hash on file: the only way
                    # this tenant gets a working key is a new one.
                    new_proxy_key = generate_proxy_key()
                    proxy_hash = hash_token(new_proxy_key)

                await conn.execute(
                    "UPDATE tenants SET api_key_hash = $2 WHERE id = $1",
                    tenant["id"],
                    proxy_hash,
                )

        needs_dashboard_token = tenant["dashboard_token_hash"] is None
        if needs_dashboard_token and not dry_run:
            new_dashboard_token = generate_dashboard_token()
            await conn.execute(
                "UPDATE tenants SET dashboard_token_hash = $2 WHERE id = $1",
                tenant["id"],
                hash_token(new_dashboard_token),
            )

        if needs_new_proxy_key or needs_dashboard_token:
            issued.append(
                Issued(
                    tenant["id"],
                    tenant["name"],
                    new_proxy_key,
                    new_dashboard_token,
                    needs_new_proxy_key,
                    needs_dashboard_token,
                )
            )

    if rehashed_in_place and not rotate:
        verb = "Would rehash" if dry_run else "Rehashed"
        print(f"\n{verb} existing proxy keys in place for tenant ids: {rehashed_in_place}")
        print("  Those keys are unchanged and keep working.")

    if issued:
        print("\n" + "=" * 72)
        # ASCII only in console output: this block is credentials the operator has
        # to read and copy, and a Windows cp1252 console renders an em dash as a
        # replacement character.
        if dry_run:
            print("WOULD ISSUE - dry run. No token was generated and none was stored.")
        else:
            print("NEW CREDENTIALS - shown once. Store them now; they cannot be recovered.")
        print("=" * 72)
        for entry in issued:
            print(f"\ntenant {entry.tenant_id} ({entry.name}):")
            if entry.would_issue_proxy_key:
                print(f"  proxy key       {entry.proxy_key or PENDING}")
            if entry.would_issue_dashboard_token:
                print(f"  dashboard token {entry.dashboard_token or PENDING}")
        print("\n" + "=" * 72)

        if rotate:
            if dry_run:
                print("BREAKING on the real run: proxy keys would be rotated. Every")
                print("caller holding an old sk-... key would get 401 until re-keyed.")
            else:
                print("BREAKING: proxy keys were rotated. Every caller holding an old")
                print("sk-... key will now get 401 until it is given the new one.")

    if dry_run:
        print("\nDry run - nothing was written.")
        return 0

    missing = await conn.fetchval("SELECT count(*) FROM tenants WHERE api_key_hash IS NULL")
    if missing:
        print(f"\n{missing} tenant(s) still have no api_key_hash; leaving tenants.api_key in place.")
        return 1

    if plaintext_present:
        await conn.execute("ALTER TABLE tenants DROP COLUMN api_key")
        print("\nDropped plaintext column tenants.api_key.")
    else:
        print("\nPlaintext column tenants.api_key was already gone.")

    return 0


USAGE = """\
usage: migrate_hash_keys.py --mode=in-place|rotate [--dry-run]

  --mode=in-place  Hash the existing plaintext keys. Callers keep working.
  --mode=rotate    Mint fresh sk-... keys instead. BREAKING: every existing
                   key stops working immediately.
  --dry-run        Apply schema migrations only; write no credential.

--mode is required and has no default. The plaintext column is dropped either
way, so this choice cannot be undone by re-running the script.\
"""

MODE_IN_PLACE = "in-place"
MODE_ROTATE = "rotate"


def parse_args(argv: list[str]) -> tuple[str, bool]:
    """Return (mode, dry_run), or raise ValueError with a usage message."""
    mode = None
    dry_run = False

    for arg in argv:
        if arg == "--dry-run":
            dry_run = True
        elif arg.startswith("--mode="):
            mode = arg.removeprefix("--mode=").strip()
        elif arg == "--mode":
            # `--mode rotate` (space-separated) is a plausible typo for the
            # documented form; rejecting it beats silently reading no mode.
            raise ValueError("Use --mode=in-place or --mode=rotate (with an '=').")
        elif arg == "--rotate":
            raise ValueError("--rotate has been replaced by --mode=rotate.")
        else:
            raise ValueError(f"Unknown argument: {arg}")

    if mode is None:
        raise ValueError("--mode is required.")

    if mode not in (MODE_IN_PLACE, MODE_ROTATE):
        raise ValueError(f"Unknown mode {mode!r}; expected {MODE_IN_PLACE} or {MODE_ROTATE}.")

    return mode, dry_run


async def main() -> int:
    try:
        mode, dry_run = parse_args(sys.argv[1:])
    except ValueError as exc:
        print(f"error: {exc}\n")
        print(USAGE)
        return 2

    rotate = mode == MODE_ROTATE

    print(f"Connecting to {DATABASE_URL.rsplit('@', 1)[-1]}")
    print(f"Mode: {mode}")
    if rotate:
        print("  Fresh sk-... keys will be minted. Existing keys will STOP working.")
    else:
        print("  Existing keys will be hashed in place and keep working.")
    if dry_run:
        print("Dry run: schema migrations still apply, but no credential is written.")

    try:
        conn = await asyncpg.connect(DATABASE_URL)
    except Exception as exc:
        print(f"\nCould not connect: {type(exc).__name__}: {exc}")
        return 1

    try:
        await ensure_schema(conn)
        return await migrate(conn, rotate=rotate, dry_run=dry_run)
    finally:
        await conn.close()


# Guarded so tests can import parse_args without the script connecting to a
# database and exiting the interpreter.
if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
