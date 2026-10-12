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
 * Requests go to this same-origin prefix, served by app/gateway/[...path]/route.ts,
 * which forwards to GATEWAY_URL and attaches the dashboard bearer token server-side.
 * The browser never holds a credential, and there is no cross-origin request to
 * preflight.
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
  /** Upstream models that actually served this tenant, from `requests.model`. */
  models: { model: string; requests: number }[];
  /**
   * The gateway's own flat-rate blend (COST_PER_1K_TOKENS). Kept for API
   * compatibility but not displayed: it prices input and output identically, so
   * it disagrees with the per-model figure the panel shows and two different
   * dollar amounts for one quantity is worse than one.
   */
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
    // A 401 here means the configured GATEWAY_DASHBOARD_TOKEN is wrong or is a
    // proxy key rather than a dash_... token — worth saying so plainly.
    const hint =
      res.status === 401
        ? " — check GATEWAY_DASHBOARD_TOKEN is a valid dash_... token"
        : "";
    throw new Error(`Gateway returned ${res.status} for ${path}${hint}`);
  }

  return (await res.json()) as T;
}

export interface DashboardData {
  stats: Stats;
  trend: TrendPoint[];
  topCached: TopCachedEntry[];
}

/**
 * There is no tenant parameter: the gateway derives the tenant from the dashboard
 * token, so a deployment shows exactly the one tenant its token belongs to. To look
 * at a different tenant, change GATEWAY_DASHBOARD_TOKEN — which is the point, since
 * the old `?tenant_id=` let anyone read any tenant's numbers.
 */
export async function fetchDashboardData(signal?: AbortSignal): Promise<DashboardData> {
  const [stats, trend, topCached] = await Promise.all([
    getJson<Stats>("/v1/stats", signal),
    getJson<{ trend: TrendPoint[] }>("/v1/stats/trend", signal),
    getJson<{ entries: TopCachedEntry[] }>("/v1/stats/top-cached", signal),
  ]);

  return { stats, trend: trend.trend ?? [], topCached: topCached.entries ?? [] };
}
