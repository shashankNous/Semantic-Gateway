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

Embedding width. `002_semantic_cache.sql` declares `vector(1536)`, the width of the
production embedding model, and the file is left exactly as it is so the README's
`psql < migrations/...` route keeps working. When `EMBEDDING_DIM` is set to
something else (local testing against an Ollama embedding model), these helpers
rewrite that one literal before executing. At the default the SQL sent is
byte-for-byte the file on disk.
"""

import pathlib

from app.services.embeddings import DEFAULT_EMBEDDING_DIM, EMBEDDING_DIM

#: pgvector's HNSW index (built on cache_entries.embedding in 002) refuses any
#: `vector` column wider than this.
HNSW_MAX_DIM = 2000

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


def render_migration(sql: str, embedding_dim: int | None = None) -> str:
    """The SQL to execute for one migration, with the vector width substituted.

    A no-op unless `embedding_dim` differs from the production width.
    """
    dim = EMBEDDING_DIM if embedding_dim is None else embedding_dim

    if dim == DEFAULT_EMBEDDING_DIM:
        return sql

    if not 0 < dim <= HNSW_MAX_DIM:
        raise ValueError(
            f"EMBEDDING_DIM={dim} is not supported: the HNSW index on "
            f"cache_entries.embedding allows 1..{HNSW_MAX_DIM} dimensions"
        )

    return sql.replace(f"vector({DEFAULT_EMBEDDING_DIM})", f"vector({dim})")


async def apply_rerunnable_migrations(
    conn,
    migrations_dir: pathlib.Path | None = None,
    embedding_dim: int | None = None,
) -> list[str]:
    """Apply every idempotent migration in order. Returns the filenames applied.

    Safe against a fully migrated database: each statement carries its own
    `IF NOT EXISTS` guard, so this re-applies to a no-op rather than erroring.
    Note that this also means a changed `EMBEDDING_DIM` does *not* resize an
    existing `cache_entries` table; the app's startup check reports the mismatch.
    """
    applied = []
    for path in rerunnable_migrations(migrations_dir):
        await conn.execute(render_migration(path.read_text(encoding="utf-8"), embedding_dim))
        applied.append(path.name)
    return applied
