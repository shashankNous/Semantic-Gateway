-- The entity/number-swap guardrail (app/services/match_verification.py) relies on
-- capitalization to spot proper nouns, which normalize_query() strips. Keep the raw
-- user query alongside the normalized one so verification has something to work with.
-- Nullable on purpose: rows written before this migration have no raw query, and the
-- guardrail falls back to normalized_query for them.
ALTER TABLE cache_entries
    ADD COLUMN IF NOT EXISTS raw_query TEXT;
