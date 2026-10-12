-- Audit trail for every call to /v1/stats*.
--
-- Deliberately not folded into `requests`: that table is the cache-performance log
-- (prompt_hash, cache_status, similarity, tokens) and every one of those columns
-- would be NULL for a stats read. It is also what /v1/stats itself aggregates, so
-- logging stats reads into it would make the hit-rate numbers count dashboard
-- traffic as gateway traffic.
--
-- tenant_id is nullable on purpose: a rejected request (401/403) has no tenant but
-- is exactly the kind of access worth having a record of.
CREATE TABLE IF NOT EXISTS stats_access_log (
    id BIGSERIAL PRIMARY KEY,
    timestamp TIMESTAMPTZ NOT NULL DEFAULT now(),
    tenant_id INT REFERENCES tenants(id),
    endpoint TEXT NOT NULL,
    requester_ip TEXT,
    status_code INT
);

CREATE INDEX IF NOT EXISTS stats_access_log_tenant_time_idx
    ON stats_access_log (tenant_id, timestamp DESC);

CREATE INDEX IF NOT EXISTS stats_access_log_time_idx
    ON stats_access_log (timestamp DESC);
