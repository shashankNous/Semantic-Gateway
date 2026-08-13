"use client";

import type { Stats } from "@/lib/gateway";
import {
  MODELS,
  computeSavings,
  formatTokens,
  formatUsd,
  getModel,
} from "@/lib/pricing";
import { Card, EmptyState, Skeleton, Stat } from "@/components/ui";

export function SavingsPanel({
  stats,
  loading,
  modelId,
  onModelChange,
}: {
  stats: Stats | null;
  loading: boolean;
  modelId: string;
  onModelChange: (id: string) => void;
}) {
  const model = getModel(modelId);

  const modelPicker = (
    <label className="flex items-center gap-2 text-sm">
      <span className="text-sidecar/75">Priced as</span>
      <select
        value={modelId}
        onChange={(event) => onModelChange(event.target.value)}
        className="rounded-md border border-sidecar/25 bg-brand-deep px-3 py-1.5 text-sm text-sidecar outline-none transition focus:border-sidecar"
      >
        {MODELS.map((option) => (
          <option key={option.id} value={option.id}>
            {option.label}
          </option>
        ))}
      </select>
    </label>
  );

  if (loading) {
    return (
      <Card title="Saved by caching" action={modelPicker}>
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
      <Card title="Saved by caching" action={modelPicker}>
        <EmptyState
          title="No requests yet"
          hint="Send some traffic through the gateway and your savings will show up here."
        />
      </Card>
    );
  }

  const { promptCost, completionCost, total } = computeSavings(
    stats.prompt_tokens_saved,
    stats.completion_tokens_saved,
    model,
  );

  return (
    <Card
      title="Saved by caching"
      subtitle={`${model.label} — $${model.inputPerMillion}/M input, $${model.outputPerMillion}/M output`}
      action={modelPicker}
    >
      <p className="text-6xl font-bold tracking-tight text-sidecar tabular-nums sm:text-7xl">
        {formatUsd(total)}
      </p>
      <p className="mt-2 text-sm text-sidecar/75">
        across {formatTokens(stats.total_requests)} requests · {(stats.hit_rate * 100).toFixed(1)}% hit rate
      </p>

      <dl className="mt-6 grid gap-3 sm:grid-cols-3">
        <Stat
          label="Prompt tokens saved"
          value={
            <>
              {formatTokens(stats.prompt_tokens_saved)}
              <span className="ml-2 text-sm font-normal text-sidecar/75">
                {formatUsd(promptCost)}
              </span>
            </>
          }
        />
        <Stat
          label="Completion tokens saved"
          value={
            <>
              {formatTokens(stats.completion_tokens_saved)}
              <span className="ml-2 text-sm font-normal text-sidecar/75">
                {formatUsd(completionCost)}
              </span>
            </>
          }
        />
        <Stat
          label="Cache hits"
          value={`${formatTokens(stats.exact_hit_count + stats.semantic_hit_count)}`}
        />
      </dl>

      <p className="mt-4 text-xs text-sidecar/75">
        Gateway&apos;s own blended estimate: {formatUsd(stats.estimated_cost_saved_usd)}
      </p>
    </Card>
  );
}
