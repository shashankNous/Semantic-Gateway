"use client";

import {
  CartesianGrid,
  Line,
  LineChart,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";

import type { TrendPoint } from "@/lib/gateway";
import { CHART } from "@/lib/theme";
import { Card, EmptyState, Skeleton } from "@/components/ui";

function formatDay(date: string): string {
  // Parse as UTC so the label matches the day the gateway bucketed it into,
  // regardless of the viewer's timezone.
  const parsed = new Date(`${date}T00:00:00Z`);
  if (Number.isNaN(parsed.getTime())) return date;

  return parsed.toLocaleDateString("en-US", {
    month: "short",
    day: "numeric",
    timeZone: "UTC",
  });
}

function ChartTooltip({
  active,
  payload,
}: {
  active?: boolean;
  payload?: Array<{ payload: TrendPoint }>;
}) {
  if (!active || !payload?.length) return null;

  const point = payload[0].payload;

  return (
    <div className="rounded-lg border border-sidecar/25 bg-brand-deep px-3 py-2 text-sm shadow-lg shadow-black/40">
      <p className="font-medium text-sidecar/70">{formatDay(point.date)}</p>
      <p className="mt-1 font-semibold text-sidecar">
        {(point.hit_rate * 100).toFixed(1)}% hit rate
      </p>
      <p className="text-sidecar/65">
        {point.hit_count} of {point.total_requests} requests
      </p>
    </div>
  );
}

export function HitRateChart({
  trend,
  loading,
}: {
  trend: TrendPoint[];
  loading: boolean;
}) {
  if (loading) {
    return (
      <Card title="Hit rate" subtitle="Last 7 days">
        <Skeleton className="h-64 w-full" />
      </Card>
    );
  }

  if (trend.length === 0) {
    return (
      <Card title="Hit rate" subtitle="Last 7 days">
        <EmptyState
          title="No traffic in the last 7 days"
          hint="Once requests start flowing, daily hit rate is plotted here."
        />
      </Card>
    );
  }

  return (
    <Card title="Hit rate" subtitle="Last 7 days">
      <div className="h-64 w-full">
        <ResponsiveContainer width="100%" height="100%">
          <LineChart data={trend} margin={{ top: 8, right: 12, bottom: 4, left: -12 }}>
            <CartesianGrid stroke={CHART.grid} vertical={false} />
            <XAxis
              dataKey="date"
              tickFormatter={formatDay}
              stroke={CHART.axis}
              tickLine={false}
              axisLine={false}
              fontSize={12}
            />
            <YAxis
              domain={[0, 1]}
              tickFormatter={(value: number) => `${Math.round(value * 100)}%`}
              stroke={CHART.axis}
              tickLine={false}
              axisLine={false}
              fontSize={12}
              width={48}
            />
            <Tooltip content={<ChartTooltip />} cursor={{ stroke: CHART.cursor }} />
            <Line
              type="monotone"
              dataKey="hit_rate"
              stroke={CHART.line}
              strokeWidth={2}
              dot={{ r: 3, fill: CHART.line, strokeWidth: 0 }}
              activeDot={{ r: 5 }}
              isAnimationActive={false}
            />
          </LineChart>
        </ResponsiveContainer>
      </div>
    </Card>
  );
}
