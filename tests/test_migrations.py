"""Static checks on the migration files, and on how the scripts apply them.

These do not execute SQL — they guard the two properties the deployment process
depends on:

  * every migration after 001 must be safe to re-run, because the README, the seed
    script, and scripts/migrate_hash_keys.py all apply them unconditionally; and
  * the scripts must discover migrations by globbing the directory rather than
    naming files, or a newly added one is silently skipped and the schema drifts.
"""

import asyncio
import pathlib
import re

import pytest

from scripts._migrations import (
    FIRST_MIGRATION,
    apply_rerunnable_migrations,
    rerunnable_migrations,
)

SCRIPTS = pathlib.Path(__file__).resolve().parent.parent / "scripts"

MIGRATIONS = pathlib.Path(__file__).resolve().parent.parent / "migrations"

# 001 predates the convention and is only ever applied to a fresh database.
RERUNNABLE = sorted(p for p in MIGRATIONS.glob("*.sql") if not p.name.startswith("001_"))


def statements(path: pathlib.Path) -> list[str]:
    """Top-level statements, with comments and DO $$ … $$ blocks stripped out."""
    sql = path.read_text(encoding="utf-8")
    sql = re.sub(r"--[^\n]*", "", sql)
    sql = re.sub(r"DO \$\$.*?\$\$;", "", sql, flags=re.DOTALL)
    return [s.strip() for s in sql.split(";") if s.strip()]


def test_there_are_migrations_to_check():
    assert [p.name for p in RERUNNABLE] == [
        "002_semantic_cache.sql",
        "003_raw_query.sql",
        "004_cache_entry_stats.sql",
        "005_token_scopes.sql",
        "006_stats_access_log.sql",
    ]


@pytest.mark.parametrize("path", RERUNNABLE, ids=lambda p: p.name)
def test_every_statement_is_idempotent(path):
    for statement in statements(path):
        upper = " ".join(statement.upper().split())

        if upper.startswith("ALTER TABLE"):
            # Each ADD COLUMN in a multi-column ALTER needs its own guard.
            adds = upper.count("ADD COLUMN")
            assert adds, f"{path.name}: unguarded ALTER TABLE: {statement[:80]}"
            assert upper.count("ADD COLUMN IF NOT EXISTS") == adds, (
                f"{path.name}: ADD COLUMN without IF NOT EXISTS: {statement[:80]}"
            )
        elif upper.startswith(("CREATE TABLE", "CREATE INDEX", "CREATE UNIQUE INDEX",
                               "CREATE EXTENSION")):
            assert "IF NOT EXISTS" in upper, (
                f"{path.name}: CREATE without IF NOT EXISTS: {statement[:80]}"
            )
        else:
            pytest.fail(f"{path.name}: unreviewed statement type: {statement[:80]}")


def test_004_adds_both_similarity_columns_as_nullable_doubles():
    sql = (MIGRATIONS / "004_cache_entry_stats.sql").read_text(encoding="utf-8")

    for column in ("min_similarity", "max_similarity"):
        assert f"{column} DOUBLE PRECISION" in sql
        # Nullable is load-bearing: an entry that has never been hit has no
        # observed similarity, and NOT NULL would need a backfill value that
        # would be indistinguishable from a real reading.
        assert f"{column} DOUBLE PRECISION NOT NULL" not in sql


def test_005_indexes_the_hash_columns_not_a_plaintext_one():
    sql = (MIGRATIONS / "005_token_scopes.sql").read_text(encoding="utf-8")

    assert "api_key_hash TEXT" in sql
    assert "dashboard_token_hash TEXT" in sql
    # Partial, because both columns are NULL between this migration and the
    # backfill, and several NULLs must not collide.
    assert sql.count("WHERE api_key_hash IS NOT NULL") == 1
    assert sql.count("WHERE dashboard_token_hash IS NOT NULL") == 1


# --- How the scripts apply them ----------------------------------------------


def test_the_rerunnable_set_is_the_directory_minus_001():
    assert [p.name for p in rerunnable_migrations()] == [p.name for p in RERUNNABLE]


def test_a_newly_added_migration_is_picked_up_without_a_code_change(tmp_path):
    """The maintenance trap this replaced: 007 existing but never being applied."""
    for name in (FIRST_MIGRATION, "005_token_scopes.sql", "007_something_new.sql"):
        (tmp_path / name).write_text("-- " + name + "\n", encoding="utf-8")

    found = [p.name for p in rerunnable_migrations(tmp_path)]

    assert found == ["005_token_scopes.sql", "007_something_new.sql"]
    assert FIRST_MIGRATION not in found


def test_migrations_are_applied_in_filename_order(tmp_path):
    for name in ("002_b.sql", "010_c.sql", "003_a.sql"):
        (tmp_path / name).write_text("SELECT '" + name + "';\n", encoding="utf-8")

    executed = []

    class RecordingConn:
        async def execute(self, sql):
            executed.append(sql.strip())

    applied = asyncio.run(apply_rerunnable_migrations(RecordingConn(), tmp_path))

    # Lexicographic order is the apply order, which is why 010 sorts last rather
    # than between 002 and 003.
    assert applied == ["002_b.sql", "003_a.sql", "010_c.sql"]
    assert executed == ["SELECT '002_b.sql';", "SELECT '003_a.sql';", "SELECT '010_c.sql';"]


@pytest.mark.parametrize("script", ["migrate_hash_keys.py", "seed_demo.py"])
def test_no_script_names_a_migration_other_than_the_first(script):
    """Both scripts glob the directory; only 001 may be referenced by name.

    001 is the exception because it predates the idempotency convention and is
    applied only to a fresh database.
    """
    source = (SCRIPTS / script).read_text(encoding="utf-8")

    named = set(re.findall(r"\b\d{3}_[a-z_]+\.sql\b", source))

    assert named <= {FIRST_MIGRATION}, (
        f"{script} names migration file(s) {sorted(named - {FIRST_MIGRATION})}; "
        "apply them by globbing migrations/ instead, or a future migration is skipped"
    )
    assert "apply_rerunnable_migrations" in source
