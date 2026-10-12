"use client";

import type { Stats } from "@/lib/gateway";
import {
  computeSavings,
  dominantModel,
  formatTokens,
  formatUsd,
  getPricing,
} from "@/lib/pricing";
import { Card, EmptyState, Skeleton, Stat } from "@/components/ui";

/**
 * What the cache avoided spending, priced against the model that actually served
 * the traffic.
 *
 * Every figure here traces to a recorded request: token counts come from the
 * upstream `usage` block on the responses the cache replayed, and the model comes
 * from `requests.model`. There is no "what if you used a different model" mode —
 * that produced a headline number that changed with a dropdown while the
 * underlying traffic stayed the same.
 */
export function SavingsPanel({
  stats,
  loading,
}: {
  stats: Stats | null;
  loading: boolean;
}) {
  if (loading) {
    return (
      <Card title="Saved by caching">
        <Skeleton className="h-20 w-72" />
        <div className="mt-6 grid gap-3 sm:grid-cols-3">
          <Skeleton className="h-16" />
          <Skeleton className="h-16" />
          <Skeleton className="h-16" />
        </div>
      </Card>
    );
  }

  if (!stats || stats.total_requests === 0) {
    return (
      <Card title="Saved by caching">
        <EmptyState
          title="No requests yet"
          hint="Send some traffic through the gateway and your savings will show up here."
        />
      </Card>
    );
  }

  const observed = dominantModel(stats.models);
  const pricing = getPricing(observed?.model ?? null);
  const mixed = (stats.models?.length ?? 0) > 1;

  const tokensSaved = stats.prompt_tokens_saved + stats.completion_tokens_saved;
  const savings = pricing
    ? computeSavings(stats.prompt_tokens_saved, stats.completion_tokens_saved, pricing)
    : null;

  const subtitle = observed
    ? pricing
      ? `${observed.model} · $${pricing.inputPerMillion}/M input, $${pricing.outputPerMillion}/M output`
      : `${observed.model} · no published rate on file, so tokens only`
    : undefined;

  return (
    <Card title="Saved by caching" subtitle={subtitle}>
      {savings ? (
        <p className="text-6xl font-bold tracking-tight text-sidecar tabular-nums sm:text-7xl">
          {formatUsd(savings.total)}
        </p>
      ) : (
        <p className="text-6xl font-bold tracking-tight text-sidecar tabular-nums sm:text-7xl">
          {formatTokens(tokensSaved)}
          <span className="ml-3 text-2xl font-normal text-sidecar/75">tokens</span>
        </p>
      )}

      <p className="mt-2 text-sm text-sidecar/75">
        across {formatTokens(stats.total_requests)} requests ·{" "}
        {(stats.hit_rate * 100).toFixed(1)}% hit rate
      </p>

      <dl className="mt-6 grid gap-3 sm:grid-cols-3">
        <Stat
          label="Prompt tokens saved"
          value={
            <>
              {formatTokens(stats.prompt_tokens_saved)}
              {savings && (
                <span className="ml-2 text-sm font-normal text-sidecar/75">
                  {formatUsd(savings.promptCost)}
                </span>
              )}
            </>
          }
        />
        <Stat
          label="Completion tokens saved"
          value={
            <>
              {formatTokens(stats.completion_tokens_saved)}
              {savings && (
                <span className="ml-2 text-sm font-normal text-sidecar/75">
                  {formatUsd(savings.completionCost)}
                </span>
              )}
            </>
          }
        />
        <Stat
          label="Cache hits"
          value={`${formatTokens(
            stats.exact_hit_count + stats.semantic_hit_count,
          )}`}
        />
      </dl>

      {mixed && (
        <p className="mt-4 text-xs text-sidecar/75">
          Traffic spans {stats.models.length} models (
          {stats.models.map((m) => `${m.model} x${m.requests}`).join(", ")}). Priced
          against {observed?.model}, so this is approximate.
        </p>
      )}
    </Card>
  );
}
