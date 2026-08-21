"""Applying the SQL files in `migrations/`, shared by the two operator scripts.

There is no `schema_migrations` tracking table in this project. What stands in for
one is a convention that `tests/test_migrations.py` enforces statically: every
migration after `001` is written to be idempotent (`ADD COLUMN IF NOT EXISTS`,
`CREATE ... IF NOT EXISTS`), so re-applying the whole set in order is a no-op on a
database that already has it.

That convention is why these helpers glob the directory rather than naming files.
An earlier version of both scripts listed `005_token_scopes.sql` and
`006_stats_access_log.sql` by hand, which meant a later `007_*.sql` would have been
silently skipped by both — the schema would drift with nothing reporting it.

`001_migrations.sql` is the one exclusion. It predates the convention (bare
`CREATE TABLE`) and errors on any database that already has the tables, so each
caller applies it separately behind a fresh-database check, or not at all.

Adding a migration therefore needs no change here: drop `007_*.sql` in
`migrations/`, keep it idempotent, and both scripts pick it up. A migration that is
*not* safe to re-run does not belong in this directory without the same kind of
guard `001` gets.
"""

import pathlib

#: Applied only to a fresh database, by whichever caller creates one. Not part of
#: the re-runnable set below.
FIRST_MIGRATION = "001_migrations.sql"

MIGRATIONS_DIR = pathlib.Path(__file__).resolve().parent.parent / "migrations"


def rerunnable_migrations(migrations_dir: pathlib.Path | None = None) -> list[pathlib.Path]:
    """Every idempotent migration file, in filename order.

    Filename order is the apply order, which is why the files are numbered. Sorting
    is lexicographic, so a tenth migration must be named `010_...`, not `10_...`.
    """
    directory = MIGRATIONS_DIR if migrations_dir is None else migrations_dir
    return sorted(p for p in directory.glob("*.sql") if p.name != FIRST_MIGRATION)


async def apply_rerunnable_migrations(conn, migrations_dir: pathlib.Path | None = None) -> list[str]:
    """Apply every idempotent migration in order. Returns the filenames applied.

    Safe against a fully migrated database: each statement carries its own
    `IF NOT EXISTS` guard, so this re-applies to a no-op rather than erroring.
    """
    applied = []
    for path in rerunnable_migrations(migrations_dir):
        await conn.execute(path.read_text(encoding="utf-8"))
        applied.append(path.name)
    return applied
