ALTER TABLE tenants
    ADD COLUMN IF NOT EXISTS uuid UUID NOT NULL DEFAULT gen_random_uuid();

CREATE UNIQUE INDEX IF NOT EXISTS tenants_uuid_idx ON tenants (uuid);

CREATE TABLE IF NOT EXISTS cache_entries (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id UUID NOT NULL REFERENCES tenants(uuid),
    normalized_query TEXT NOT NULL,
    prompt_hash TEXT NOT NULL,
    response_json JSONB NOT NULL,
    embedding vector(1536) NOT NULL,
    model TEXT,
    hit_count INTEGER DEFAULT 0,
    created_at TIMESTAMP DEFAULT now()
);

CREATE INDEX IF NOT EXISTS cache_entries_embedding_hnsw_idx
    ON cache_entries
    USING hnsw (embedding vector_cosine_ops);

CREATE INDEX IF NOT EXISTS cache_entries_tenant_id_idx
    ON cache_entries (tenant_id);

ALTER TABLE requests
    ADD COLUMN IF NOT EXISTS cache_status TEXT,
    ADD COLUMN IF NOT EXISTS similarity DOUBLE PRECISION;
