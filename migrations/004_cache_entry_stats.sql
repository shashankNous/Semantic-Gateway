-- Track the similarity range a cache entry has actually served over its lifetime.
-- Both columns are nullable: an entry that has never been hit has no observed
-- similarity yet, and rows written before this migration have no history to
-- backfill from. find_semantic_match() seeds them with LEAST/GREATEST + COALESCE
-- on the first hit, so a NULL simply means "no hits recorded".
ALTER TABLE cache_entries
    ADD COLUMN IF NOT EXISTS min_similarity DOUBLE PRECISION,
    ADD COLUMN IF NOT EXISTS max_similarity DOUBLE PRECISION;
