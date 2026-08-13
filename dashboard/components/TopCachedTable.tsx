"use client";

import type { TopCachedEntry } from "@/lib/gateway";
import { Card, EmptyState, Skeleton } from "@/components/ui";

/**
 * min/max similarity are NULL until an entry has actually been served from
 * cache at least once (see migrations/004_cache_entry_stats.sql), so a
 * never-hit row renders a dash rather than a bogus 0.00 range.
 */
function similarityRange(entry: TopCachedEntry): string {
  const { min_similarity: min, max_similarity: max } = entry;

  if (min === null || max === null) return "—";
  if (min === max) return min.toFixed(3);

  return `${min.toFixed(3)}–${max.toFixed(3)}`;
}

export function TopCachedTable({
  entries,
  loading,
}: {
  entries: TopCachedEntry[];
  loading: boolean;
}) {
  if (loading) {
    return (
      <Card title="Top cached queries" subtitle="Ranked by hit count">
        <div className="space-y-2">
          {Array.from({ length: 6 }).map((_, index) => (
            <Skeleton key={index} className="h-9 w-full" />
          ))}
        </div>
      </Card>
    );
  }

  if (entries.length === 0) {
    return (
      <Card title="Top cached queries" subtitle="Ranked by hit count">
        <EmptyState
          title="Nothing cached yet"
          hint="Cacheable requests populate the semantic cache on their first miss."
        />
      </Card>
    );
  }

  return (
    <Card title="Top cached queries" subtitle="Top 20, ranked by hit count">
      <div className="overflow-x-auto">
        <table className="w-full min-w-[36rem] text-left text-sm">
          <thead>
            <tr className="border-b border-sidecar/20 text-xs uppercase tracking-wide text-sidecar/75">
              <th className="pb-2 pr-4 font-medium">Query</th>
              <th className="pb-2 pr-4 text-right font-medium">Hits</th>
              <th className="pb-2 text-right font-medium">Similarity range</th>
            </tr>
          </thead>
          <tbody className="divide-y divide-sidecar/10">
            {entries.map((entry, index) => (
              <tr
                key={`${entry.normalized_query}-${index}`}
                className="transition hover:bg-sidecar/5"
              >
                <td
                  className="max-w-md truncate py-2.5 pr-4 text-sidecar/90"
                  title={entry.normalized_query}
                >
                  {entry.normalized_query}
                </td>
                <td className="py-2.5 pr-4 text-right font-semibold tabular-nums text-sidecar">
                  {entry.hit_count}
                </td>
                <td className="py-2.5 text-right tabular-nums text-sidecar/75">
                  {similarityRange(entry)}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </Card>
  );
}
