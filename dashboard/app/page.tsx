"use client";

import { useCallback, useEffect, useState } from "react";

import { GATEWAY_URL, fetchDashboardData, type DashboardData } from "@/lib/gateway";
import { DEFAULT_MODEL_ID } from "@/lib/pricing";
import { HitRateChart } from "@/components/HitRateChart";
import { SavingsPanel } from "@/components/SavingsPanel";
import { TopCachedTable } from "@/components/TopCachedTable";

export default function DashboardPage() {
  const [modelId, setModelId] = useState(DEFAULT_MODEL_ID);
  const [data, setData] = useState<DashboardData | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async (signal?: AbortSignal) => {
    setLoading(true);
    setError(null);

    try {
      setData(await fetchDashboardData(signal));
    } catch (err) {
      if (signal?.aborted) return;
      setData(null);
      setError(
        err instanceof Error
          ? `${err.message} (gateway: ${GATEWAY_URL})`
          : "Could not reach the gateway.",
      );
    } finally {
      if (!signal?.aborted) setLoading(false);
    }
  }, []);

  useEffect(() => {
    const controller = new AbortController();
    void load(controller.signal);
    return () => controller.abort();
  }, [load]);

  return (
    <main className="mx-auto min-h-screen w-full max-w-5xl px-4 py-10 sm:px-6 sm:py-14">
      <header className="mb-8 flex flex-wrap items-end justify-between gap-4">
        <div>
          <h1 className="text-2xl font-semibold tracking-tight text-sidecar">
            Semantic Gateway
          </h1>
          <p className="mt-1 text-sm text-sidecar/65">
            Cache performance and cost avoided ·{" "}
            <code className="text-sidecar/80">{GATEWAY_URL}</code>
            {data && (
              <>
                {" · tenant "}
                <code className="text-sidecar/80">{data.stats.tenant_id}</code>
              </>
            )}
          </p>
        </div>

        {/* No tenant picker: the gateway resolves the tenant from the dashboard
            token, so this deployment can only ever show its own. */}
        <div className="flex items-end gap-2">
          <button
            type="button"
            onClick={() => void load()}
            className="rounded-md border border-sidecar/25 px-3 py-1.5 text-sm text-sidecar/80 transition hover:border-sidecar/60 hover:bg-sidecar/10 hover:text-sidecar"
          >
            Refresh
          </button>
        </div>
      </header>

      {error && (
        <div className="mb-6 rounded-lg border border-rose-300/40 bg-rose-950/50 px-4 py-3 text-sm text-rose-100">
          {error}
        </div>
      )}

      <div className="space-y-6">
        <SavingsPanel
          stats={data?.stats ?? null}
          loading={loading}
          modelId={modelId}
          onModelChange={setModelId}
        />
        <HitRateChart trend={data?.trend ?? []} loading={loading} />
        <TopCachedTable entries={data?.topCached ?? []} loading={loading} />
      </div>
    </main>
  );
}
