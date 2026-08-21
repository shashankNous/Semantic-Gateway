"""The startup guard that refuses to serve traffic before the credential backfill.

`app/db.py` authenticates against the hash columns only, with no plaintext
fallback, so an app started against a half-migrated database 401s every request
instead of failing visibly. `verify_auth_schema` is what converts that into one
startup error, and these tests pin which states it refuses and which it tolerates.
"""

import asyncio
import logging

import asyncpg
import pytest

import app.db as db


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


class FakeConn:
    """Returns `row`, or raises `raises` when the count query runs."""

    def __init__(self, row=None, raises=None):
        self.row = row
        self.raises = raises

    async def fetchrow(self, *args):
        if self.raises is not None:
            raise self.raises
        return self.row


def counts(total, missing_proxy=0, missing_dashboard=0):
    return {
        "total": total,
        "missing_proxy": missing_proxy,
        "missing_dashboard": missing_dashboard,
    }


@pytest.fixture
def use_pool(monkeypatch):
    def install(conn):
        monkeypatch.setattr(db, "pool", FakePool(conn))

    return install


def run(coro):
    return asyncio.run(coro)


def test_a_fully_backfilled_database_starts(use_pool):
    use_pool(FakeConn(counts(3)))

    run(db.verify_auth_schema())  # no raise


def test_an_empty_tenants_table_is_not_an_error(use_pool):
    """A fresh install has nothing to backfill; that must not block startup."""
    use_pool(FakeConn(counts(0)))

    run(db.verify_auth_schema())


def test_missing_hash_columns_name_the_migration_and_the_backfill(use_pool):
    use_pool(FakeConn(raises=asyncpg.UndefinedColumnError("no such column")))

    with pytest.raises(RuntimeError) as excinfo:
        run(db.verify_auth_schema())

    message = str(excinfo.value)
    assert "005_token_scopes.sql" in message
    assert "migrate_hash_keys.py" in message


def test_a_missing_tenants_table_is_reported_as_missing_migrations(use_pool):
    use_pool(FakeConn(raises=asyncpg.UndefinedTableError("no such table")))

    with pytest.raises(RuntimeError, match="tenants"):
        run(db.verify_auth_schema())


def test_an_unbackfilled_proxy_hash_refuses_to_start(use_pool):
    """Columns exist but migrate_hash_keys.py never ran: every proxy call would 401."""
    use_pool(FakeConn(counts(4, missing_proxy=2)))

    with pytest.raises(RuntimeError) as excinfo:
        run(db.verify_auth_schema())

    message = str(excinfo.value)
    assert "2 of 4" in message
    assert "migrate_hash_keys.py" in message


def test_a_missing_dashboard_token_warns_rather_than_blocking(use_pool, caplog):
    """Only /v1/stats* is affected, so the gateway still comes up."""
    use_pool(FakeConn(counts(4, missing_dashboard=1)))

    with caplog.at_level(logging.WARNING, logger=db.__name__):
        run(db.verify_auth_schema())

    assert "dashboard_token_hash" in caplog.text


def test_it_needs_a_pool(monkeypatch):
    monkeypatch.setattr(db, "pool", None)

    with pytest.raises(RuntimeError, match="pool is not initialized"):
        run(db.verify_auth_schema())


def test_startup_runs_the_check_after_opening_the_pool(monkeypatch):
    """Ordering matters: the check needs the pool the previous step creates."""
    import app.main as main

    calls = []

    async def fake_init():
        calls.append("init_db_pool")

    async def fake_verify():
        calls.append("verify_auth_schema")

    monkeypatch.setattr(main, "init_db_pool", fake_init)
    monkeypatch.setattr(main, "verify_auth_schema", fake_verify)

    run(main.startup())

    assert calls == ["init_db_pool", "verify_auth_schema"]
