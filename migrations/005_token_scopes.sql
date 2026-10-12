-- Split one tenant credential into two scoped ones, and stop storing either in
-- plaintext.
--
-- Schema choice: a second column rather than a `scope` column with one row per
-- token. `requests.tenant_id` references `tenants.id` and `cache_entries.tenant_id`
-- references `tenants.uuid`, so a tenant *is* a row here — issuing a second row per
-- tenant to represent a second token would fork that identity and break both foreign
-- keys. Two nullable hash columns keep one row = one tenant, and the scope of a
-- presented token is simply "which column did it match".
--
-- Both columns are nullable so this migration can be applied before the backfill
-- (scripts/migrate_hash_keys.py) has run. That script hashes the existing plaintext
-- `api_key` into `api_key_hash`, issues a `dash_...` token per tenant, and then drops
-- the plaintext `api_key` column. Until it runs, authentication will fail closed —
-- no request is ever authorized off the plaintext column again.
ALTER TABLE tenants
    ADD COLUMN IF NOT EXISTS api_key_hash TEXT,
    ADD COLUMN IF NOT EXISTS dashboard_token_hash TEXT;

-- Partial unique indexes: NULL is expected during the backfill window, and several
-- tenants sitting at NULL must not collide with each other.
CREATE UNIQUE INDEX IF NOT EXISTS tenants_api_key_hash_idx
    ON tenants (api_key_hash)
    WHERE api_key_hash IS NOT NULL;

CREATE UNIQUE INDEX IF NOT EXISTS tenants_dashboard_token_hash_idx
    ON tenants (dashboard_token_hash)
    WHERE dashboard_token_hash IS NOT NULL;
