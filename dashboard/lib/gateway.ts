/**
 * Thin client for the FastAPI gateway's stats endpoints.
 *
 * The dashboard never talks to Postgres directly — everything comes over HTTP
 * from the gateway, so the only thing this app needs to know is its base URL.
 */

/** The configured gateway, shown in the UI so it is obvious what is being read. */
export const GATEWAY_URL = (
  process.env.NEXT_PUBLIC_GATEWAY_URL || "http://localhost:8000"
).replace(/\/+$/, "");

/**
 * Requests go to this same-origin prefix, which next.config.mjs rewrites to
 * GATEWAY_URL on the server. Same destination, minus the CORS preflight the
 * gateway is not set up to answer.
 */
const API_BASE = "/gateway";

export interface Stats {
  tenant_id: number;
  total_requests: number;
  exact_hit_count: number;
  semantic_hit_count: number;
  miss_count: number;
  uncacheable_count: number;
  hit_rate: number;
  avg_latency_ms_by_cache_status: Record<string, number | null>;
  prompt_tokens_saved: number;
  completion_tokens_saved: number;
  estimated_cost_saved_usd: number;
}

export interface TrendPoint {
  date: string;
  total_requests: number;
  hit_count: number;
  hit_rate: number;
}

export interface TopCachedEntry {
  normalized_query: string;
  hit_count: number;
  min_similarity: number | null;
  max_similarity: number | null;
}

async function getJson<T>(path: string, signal?: AbortSignal): Promise<T> {
  const res = await fetch(`${API_BASE}${path}`, {
    signal,
    cache: "no-store",
    headers: { Accept: "application/json" },
  });

  if (!res.ok) {
    throw new Error(`Gateway returned ${res.status} for ${path}`);
  }

  return (await res.json()) as T;
}

export interface DashboardData {
  stats: Stats;
  trend: TrendPoint[];
  topCached: TopCachedEntry[];
}

export async function fetchDashboardData(
  tenantId: number,
  signal?: AbortSignal,
): Promise<DashboardData> {
  const query = `?tenant_id=${encodeURIComponent(tenantId)}`;

  const [stats, trend, topCached] = await Promise.all([
    getJson<Stats>(`/v1/stats${query}`, signal),
    getJson<{ trend: TrendPoint[] }>(`/v1/stats/trend${query}`, signal),
    getJson<{ entries: TopCachedEntry[] }>(`/v1/stats/top-cached${query}`, signal),
  ]);

  return { stats, trend: trend.trend ?? [], topCached: topCached.entries ?? [] };
}
