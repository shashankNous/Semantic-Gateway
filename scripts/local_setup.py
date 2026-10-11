"""Bring up the local test stack from scratch and issue test credentials.

    python scripts/local_setup.py

LOCAL ONLY. Every run starts from an empty database, because credentials are
stored as hashes and cannot be recovered from a previous run. In order:

1. Checks `.env.local` points only at this machine (scripts/_local_env.py).
2. Calls the embedding model once and checks it returns EMBEDDING_DIM numbers,
   so a wrong dimension fails here rather than at the first cache insert.
3. Starts docker-compose.local.yml (Postgres + pgvector, Redis) and waits for it.
4. Drops and recreates the `public` schema in the local database.
5. Applies migrations 001-006 as the `postgres` master user, with the vector
   column sized to EMBEDDING_DIM (scripts/_migrations.py).
6. Creates three test tenants with freshly generated `sk-...` keys in the
   plaintext column, then runs `migrate_hash_keys.py --mode=in-place`, which
   hashes those keys, issues a `dash_...` token per tenant and drops the plaintext
   column -- the same path a real deployment takes.
7. Writes every credential to `.local-credentials.json` (gitignored), which the
   smoke test reads. Nothing is printed.

`--mode=rotate` is deliberately not used: in-place produces the same end state
here without minting keys the script would then have to scrape from output.
"""

import asyncio
import json
import pathlib
import re
import subprocess
import sys
import urllib.request
from urllib.parse import urlparse

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from scripts._local_env import (  # noqa: E402
    COMPOSE_FILE,
    CREDENTIALS_FILE,
    REPO,
    apply_to_process,
    child_env,
    load_local_env,
)

#: Local test tenants. A and B are the main and isolation tenants; C holds the
#: known-gap probes so they cannot interfere with A's cache.
TENANTS = ("Local Tenant A", "Local Tenant B", "Local Tenant C")

DASHBOARD_LINE = re.compile(r"^tenant (\d+) \((.+)\):\s*$")
TOKEN_LINE = re.compile(r"^\s+dashboard token\s+(dash_\S+)\s*$")


def step(message: str) -> None:
    print(f"\n==> {message}", flush=True)


def check_embedding_dimension(env: dict) -> None:
    expected = int(env["EMBEDDING_DIM"])
    request = urllib.request.Request(
        env["EMBEDDING_BASE_URL"].rstrip("/") + "/embeddings",
        data=json.dumps({"model": env["EMBEDDING_MODEL"], "input": "dimension check"}).encode(),
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {env['EMBEDDING_API_KEY']}",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=120) as response:
            actual = len(json.load(response)["data"][0]["embedding"])
    except Exception as exc:
        raise SystemExit(
            f"Could not embed with {env['EMBEDDING_MODEL']!r} at "
            f"{env['EMBEDDING_BASE_URL']}: {exc}. Is Ollama running, and is the model "
            "pulled (`ollama list`)?"
        )

    if actual != expected:
        raise SystemExit(
            f"{env['EMBEDDING_MODEL']} returns {actual} dimensions but EMBEDDING_DIM is "
            f"{expected}. Set EMBEDDING_DIM={actual} in .env.local."
        )
    print(f"  {env['EMBEDDING_MODEL']} returns {actual} dimensions, as configured")


def start_stack() -> None:
    subprocess.run(
        ["docker", "compose", "-f", str(COMPOSE_FILE), "up", "-d", "--wait"],
        check=True,
    )


async def reset_and_migrate(env: dict) -> dict:
    import asyncpg

    from app.security import generate_proxy_key
    from scripts._migrations import FIRST_MIGRATION, MIGRATIONS_DIR, apply_rerunnable_migrations

    url = urlparse(env["DATABASE_URL"])
    if url.username != "postgres":
        raise SystemExit("DATABASE_URL must connect as the `postgres` master user")

    conn = await asyncpg.connect(env["DATABASE_URL"])
    try:
        await conn.execute("DROP SCHEMA public CASCADE; CREATE SCHEMA public;")
        print("  dropped and recreated schema public")

        await conn.execute((MIGRATIONS_DIR / FIRST_MIGRATION).read_text(encoding="utf-8"))
        print(f"  applied {FIRST_MIGRATION}")
        for name in await apply_rerunnable_migrations(conn):
            print(f"  applied {name}")

        dim = await conn.fetchval(
            "SELECT atttypmod FROM pg_attribute "
            "WHERE attrelid = 'cache_entries'::regclass AND attname = 'embedding'"
        )
        print(f"  cache_entries.embedding is vector({dim})")

        keys = {}
        for name in TENANTS:
            key = generate_proxy_key()
            await conn.execute("INSERT INTO tenants (api_key, name) VALUES ($1, $2)", key, name)
            keys[name] = key
        print(f"  created {len(TENANTS)} tenants")
        return keys
    finally:
        await conn.close()


def hash_keys_and_issue_dashboard_tokens(env: dict) -> dict:
    result = subprocess.run(
        [sys.executable, str(REPO / "scripts" / "migrate_hash_keys.py"), "--mode=in-place"],
        env=child_env(env),
        capture_output=True,
        text=True,
    )

    tokens = {}
    current = None
    for line in result.stdout.splitlines():
        if match := DASHBOARD_LINE.match(line):
            current = match.group(2)
        elif (match := TOKEN_LINE.match(line)) and current:
            tokens[current] = match.group(1)
            line = line.replace(match.group(1), "dash_*** (saved to .local-credentials.json)")
        print(f"  | {line}")

    if result.returncode != 0:
        print(result.stderr)
        raise SystemExit(f"migrate_hash_keys.py exited {result.returncode}")

    missing = set(TENANTS) - set(tokens)
    if missing:
        raise SystemExit(f"No dashboard token captured for {sorted(missing)}")

    return tokens


def main() -> None:
    env = load_local_env()
    apply_to_process(env)

    step("Checking the embedding model")
    check_embedding_dimension(env)

    step("Starting docker-compose.local.yml")
    start_stack()

    step("Resetting the local database and applying migrations as postgres")
    keys = asyncio.run(reset_and_migrate(env))

    step("Running migrate_hash_keys.py --mode=in-place against the local database")
    tokens = hash_keys_and_issue_dashboard_tokens(env)

    credentials = {
        name: {"proxy_key": keys[name], "dashboard_token": tokens[name]} for name in TENANTS
    }
    CREDENTIALS_FILE.write_text(json.dumps(credentials, indent=2) + "\n", encoding="utf-8")

    step(f"Done. Credentials for {len(TENANTS)} tenants written to {CREDENTIALS_FILE.name}")
    print("  Next: python scripts/run_local_gateway.py   (in another terminal)")
    print("        python scripts/local_smoke_test.py")


if __name__ == "__main__":
    main()
