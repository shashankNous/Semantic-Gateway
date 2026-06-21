CREATE TABLE tenants (
    id SERIAL PRIMARY KEY,
    api_key TEXT UNIQUE NOT NULL,
    name TEXT NOT NULL,
    created_at TIMESTAMPTZ DEFAULT now()
);

CREATE TABLE requests (
    id SERIAL PRIMARY KEY,
    timestamp TIMESTAMPTZ DEFAULT now(),
    tenant_id INT REFERENCES tenants(id),
    prompt_hash TEXT,
    hit BOOLEAN,
    latency_ms INT,
    model TEXT,
    prompt_tokens INT,
    completion_tokens INT
);