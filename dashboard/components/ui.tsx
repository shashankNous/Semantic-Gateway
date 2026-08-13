import type { ReactNode } from "react";

export function Card({
  title,
  subtitle,
  action,
  children,
  className = "",
}: {
  title?: string;
  subtitle?: string;
  action?: ReactNode;
  children: ReactNode;
  className?: string;
}) {
  return (
    <section
      className={`rounded-xl border border-sidecar/15 bg-brand p-5 shadow-lg shadow-black/20 sm:p-6 ${className}`}
    >
      {(title || action) && (
        <header className="mb-5 flex flex-wrap items-start justify-between gap-3">
          <div>
            {title && (
              <h2 className="text-sm font-semibold uppercase tracking-wider text-sidecar/75">
                {title}
              </h2>
            )}
            {subtitle && <p className="mt-1 text-sm text-sidecar/75">{subtitle}</p>}
          </div>
          {action}
        </header>
      )}
      {children}
    </section>
  );
}

export function Skeleton({ className = "" }: { className?: string }) {
  return <div className={`animate-pulse rounded bg-sidecar/10 ${className}`} />;
}

export function EmptyState({
  title,
  hint,
}: {
  title: string;
  hint?: string;
}) {
  return (
    <div className="flex flex-col items-center justify-center rounded-lg border border-dashed border-sidecar/25 px-6 py-12 text-center">
      <p className="text-sm font-medium text-sidecar/80">{title}</p>
      {hint && <p className="mt-1 max-w-md text-sm text-sidecar/75">{hint}</p>}
    </div>
  );
}

export function Stat({ label, value }: { label: string; value: ReactNode }) {
  return (
    <div className="rounded-lg border border-sidecar/10 bg-brand-deep/60 px-4 py-3">
      <dt className="text-xs uppercase tracking-wide text-sidecar/75">{label}</dt>
      <dd className="mt-1 text-lg font-semibold tabular-nums text-sidecar">{value}</dd>
    </div>
  );
}
