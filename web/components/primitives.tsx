import { cn } from "@/lib/cn";
import {
  AlertCircle,
  AlertTriangle,
  Inbox,
  Lock,
  Plug,
  Search,
} from "lucide-react";

export function StatusChip({
  tone,
  children
}: {
  tone: "credit" | "debit" | "pending" | "ink" | "agent" | "blocked";
  children: React.ReactNode;
}) {
  const map: Record<string, string> = {
    credit: "bg-credit/15 text-credit",
    debit: "bg-debit/15 text-debit",
    pending: "bg-pending/15 text-pending",
    ink: "bg-white/5 text-ink-secondary",
    agent: "bg-agent-500/15 text-agent-500",
    blocked: "bg-blocked/15 text-blocked"
  };
  return (
    <span
      className={cn(
        "inline-flex items-center rounded-full px-2 py-0.5 text-[10px] font-medium uppercase tracking-wider",
        map[tone]
      )}
    >
      {children}
    </span>
  );
}

export function MaterialityBadge({ level }: { level: "L" | "M" | "H" }) {
  const map: Record<string, string> = {
    L: "bg-white/5 text-ink-secondary border-subtle",
    M: "bg-pending/15 text-pending border-pending/40",
    H: "bg-debit/15 text-debit border-debit/40"
  };
  return (
    <span
      className={cn(
        "inline-flex h-5 w-5 items-center justify-center rounded-full border font-mono text-[10px] font-medium",
        map[level]
      )}
      title={
        level === "L"
          ? "Low materiality: agent may auto-approve"
          : level === "M"
            ? "Medium materiality: queues for review"
            : "High materiality: blocks until human-attested"
      }
    >
      {level}
    </span>
  );
}

export function ConfidenceDot({ value }: { value: number }) {
  const cls =
    value >= 0.9 ? "bg-conf-high" : value >= 0.7 ? "bg-conf-medium" : "bg-conf-low";
  const label =
    value >= 0.9 ? "Confident" : value >= 0.7 ? "Some uncertainty" : "Low confidence — needs review";
  return (
    <span
      className={cn("inline-block h-1.5 w-1.5 rounded-full", cls)}
      title={`${label} (${(value * 100).toFixed(0)}%)`}
    />
  );
}

export function KpiCard({
  label,
  value,
  delta,
  deltaTone = "ink"
}: {
  label: string;
  value: string;
  delta?: string;
  deltaTone?: "credit" | "debit" | "pending" | "ink";
}) {
  const deltaMap: Record<string, string> = {
    credit: "text-credit",
    debit: "text-debit",
    pending: "text-pending",
    ink: "text-ink-tertiary"
  };
  return (
    <div className="rounded-lg border border-default bg-surface p-4 md:p-6 transition hover:border-strong">
      <p className="text-[10px] font-medium uppercase tracking-wider text-ink-tertiary">{label}</p>
      <p className="mt-2 md:mt-3 font-mono text-numeric-lg text-ink-primary break-all">{value}</p>
      {delta ? <p className={cn("mt-1 text-xs md:text-sm", deltaMap[deltaTone])}>{delta}</p> : null}
    </div>
  );
}

export function SectionHeader({
  title,
  subtitle,
  action,
  breadcrumb
}: {
  title: string;
  subtitle?: string;
  action?: React.ReactNode;
  breadcrumb?: React.ReactNode;
}) {
  return (
    <div className="mb-5 md:mb-6">
      <div className="flex flex-col gap-3 sm:flex-row sm:flex-wrap sm:items-end sm:justify-between">
        <div className="min-w-0">
          <h1 className="text-xl md:text-2xl font-semibold tracking-tight">{title}</h1>
          {subtitle ? <p className="mt-1 text-xs md:text-sm text-ink-secondary">{subtitle}</p> : null}
        </div>
        {action ? <div className="flex flex-wrap items-center gap-2">{action}</div> : null}
      </div>
      {breadcrumb ? <div className="mt-3">{breadcrumb}</div> : null}
    </div>
  );
}

export function Card({
  children,
  className,
  padded = true
}: {
  children: React.ReactNode;
  className?: string;
  padded?: boolean;
}) {
  return (
    <div
      className={cn(
        // I19: 8px corner radius for cards (rounded-lg = 0.5rem = 8px)
        "rounded-lg border border-default bg-surface",
        padded && "p-4 md:p-6",
        className
      )}
    >
      {children}
    </div>
  );
}

export function PageShell({ children }: { children: React.ReactNode }) {
  return (
    <div className="mx-auto w-full max-w-7xl px-4 py-5 md:px-6 md:py-8">
      {children}
    </div>
  );
}

// ----------------------------------------------------------------------------
// Empty / Error / Skeleton primitives (Tier-2 audit H9/H10)
// ----------------------------------------------------------------------------
//
// EmptyState
//   Shared empty-state UX. Differentiates between four meaningful "nothing
//   to show" cases so the copy + CTAs match the actual situation:
//
//     not-connected   — service prerequisite missing (e.g. "Connect a bank")
//     no-data         — service is healthy but has no rows yet
//     filtered-empty  — the user's filter excludes every row
//     permission-denied — the user is here but lacks the role to see content
//
// ErrorState
//   Shared error-state UX. Surface short user-safe copy + a recovery action,
//   with technicalDetails hidden inside a collapsed "Details" disclosure
//   for admins/developers.
//
// RouteSkeleton
//   Per-page loading skeleton variants. Used by `loading.tsx` Suspense
//   boundaries and inside server components while data is in-flight.

export type EmptyStateVariant =
  | "not-connected"
  | "no-data"
  | "filtered-empty"
  | "permission-denied";

const EMPTY_VARIANT_DEFAULT_ICON: Record<EmptyStateVariant, React.ElementType> = {
  "not-connected": Plug,
  "no-data": Inbox,
  "filtered-empty": Search,
  "permission-denied": Lock,
};

export function EmptyState({
  icon,
  title,
  description,
  primaryAction,
  secondaryAction,
  variant = "no-data",
  className,
}: {
  icon?: React.ReactNode;
  title: string;
  description?: React.ReactNode;
  primaryAction?: React.ReactNode;
  secondaryAction?: React.ReactNode;
  variant?: EmptyStateVariant;
  className?: string;
}) {
  const DefaultIcon = EMPTY_VARIANT_DEFAULT_ICON[variant];
  const renderedIcon = icon ?? (
    <DefaultIcon size={28} className="text-ink-disabled" aria-hidden="true" />
  );

  return (
    <div
      className={cn(
        // 8px radius matches updated Card primitive (I19)
        "rounded-lg border border-default bg-surface px-5 py-10 text-center",
        className
      )}
      data-empty-variant={variant}
      role="status"
    >
      <div className="mx-auto flex max-w-md flex-col items-center gap-3">
        <span className="flex h-12 w-12 items-center justify-center rounded-full bg-elevated">
          {renderedIcon}
        </span>
        <p className="text-base font-medium text-ink-primary">{title}</p>
        {description ? (
          <div className="text-xs leading-relaxed text-ink-tertiary">
            {typeof description === "string" ? <p>{description}</p> : description}
          </div>
        ) : null}
        {(primaryAction || secondaryAction) && (
          <div className="mt-2 flex flex-wrap items-center justify-center gap-2">
            {primaryAction}
            {secondaryAction}
          </div>
        )}
      </div>
    </div>
  );
}

export function ErrorState({
  title,
  description,
  recoveryAction,
  technicalDetails,
  className,
  variant = "default",
}: {
  title: string;
  description?: React.ReactNode;
  recoveryAction?: React.ReactNode;
  /** Hidden behind an admin-only Details disclosure. Use for HTTP codes,
   *  Docker commands, make targets, etc. Should NOT be visible to founders. */
  technicalDetails?: React.ReactNode;
  className?: string;
  variant?: "default" | "warning";
}) {
  const Icon = variant === "warning" ? AlertTriangle : AlertCircle;
  const tone =
    variant === "warning"
      ? "border-pending/40 bg-pending/[0.05] text-pending"
      : "border-debit/40 bg-debit/[0.05] text-debit";
  return (
    <div
      className={cn("rounded-lg border bg-surface px-5 py-6", tone, className)}
      role="alert"
    >
      <div className="flex items-start gap-3">
        <Icon size={18} className="mt-0.5 shrink-0" aria-hidden="true" />
        <div className="min-w-0 flex-1">
          <p className="text-sm font-semibold text-ink-primary">{title}</p>
          {description ? (
            <div className="mt-1 text-xs leading-relaxed text-ink-secondary">
              {typeof description === "string" ? <p>{description}</p> : description}
            </div>
          ) : null}
          {recoveryAction ? (
            <div className="mt-3 flex flex-wrap items-center gap-2">{recoveryAction}</div>
          ) : null}
          {technicalDetails ? (
            <details className="mt-3 group">
              <summary className="cursor-pointer text-[10px] font-medium uppercase tracking-wider text-ink-tertiary hover:text-ink-secondary">
                Technical details (admin only)
              </summary>
              <div className="mt-2 rounded-md border border-subtle bg-canvas/60 px-3 py-2 font-mono text-[11px] text-ink-secondary">
                {technicalDetails}
              </div>
            </details>
          ) : null}
        </div>
      </div>
    </div>
  );
}

// ----------------------------------------------------------------------------
// RouteSkeleton — per-page-shape skeleton variants
// ----------------------------------------------------------------------------
//
// Variants:
//   dashboard  — header row + 4-up KPI grid + main + side rail
//   table      — header row + filter strip + dense table rows
//   narrative  — banner + body paragraphs + companion list
//   chat       — sidebar shell + message list + composer
//   coa        — header + grouped account rows
//   reports    — header + tab pills + dense rows
//   default    — title + cards stub
//
// All skeletons use animate-pulse on neutral surfaces; explicit aria-busy
// so screen readers announce the loading state.

export type RouteSkeletonVariant =
  | "dashboard"
  | "table"
  | "narrative"
  | "chat"
  | "coa"
  | "reports"
  | "default";

function SkLine({
  width,
  height = "h-3.5",
  className,
}: {
  width: string;
  height?: string;
  className?: string;
}) {
  return (
    <div
      className={cn("rounded bg-elevated/70", width, height, className)}
      aria-hidden="true"
    />
  );
}

function SkBlock({
  className,
  children,
}: {
  className?: string;
  children?: React.ReactNode;
}) {
  return (
    <div
      className={cn("rounded-lg border border-default bg-surface", className)}
      aria-hidden="true"
    >
      {children}
    </div>
  );
}

export function RouteSkeleton({
  variant = "default",
  label,
}: {
  variant?: RouteSkeletonVariant;
  /** Accessible label announced by screen readers (e.g. "Loading dashboard"). */
  label?: string;
}) {
  const screenLabel = label ?? `Loading ${variant}`;
  return (
    <div
      className="animate-pulse"
      role="status"
      aria-busy="true"
      aria-live="polite"
    >
      <span className="sr-only">{screenLabel}</span>
      {variant === "dashboard" && (
        <>
          <div className="mb-6 flex items-center justify-between">
            <div className="space-y-2">
              <SkLine width="w-40" height="h-6" />
              <SkLine width="w-72" />
            </div>
            <SkLine width="w-36" height="h-8" className="rounded-md" />
          </div>
          <div className="grid grid-cols-1 gap-4 md:grid-cols-2 lg:grid-cols-4">
            {[0, 1, 2, 3].map((i) => (
              <SkBlock key={i} className="p-5">
                <SkLine width="w-20" height="h-3" />
                <SkLine width="w-32" height="h-7" className="mt-3" />
                <SkLine width="w-24" height="h-3" className="mt-2" />
              </SkBlock>
            ))}
          </div>
          <div className="mt-8 grid grid-cols-1 gap-4 lg:grid-cols-3">
            <SkBlock className="lg:col-span-2">
              <div className="border-b border-subtle px-5 py-4">
                <SkLine width="w-40" height="h-5" />
                <SkLine width="w-56" height="h-3" className="mt-2" />
              </div>
              <div className="space-y-3 p-5">
                {[0, 1, 2, 3, 4].map((i) => (
                  <div key={i} className="flex items-center gap-3">
                    <SkLine width="w-16" />
                    <SkLine width="flex-1" />
                    <SkLine width="w-20" />
                  </div>
                ))}
              </div>
            </SkBlock>
            <div className="space-y-4">
              <SkBlock className="p-5">
                <SkLine width="w-32" height="h-5" />
                <div className="mt-4 space-y-2">
                  {[0, 1, 2].map((i) => (
                    <SkLine key={i} width="w-full" />
                  ))}
                </div>
              </SkBlock>
              <SkBlock className="p-5">
                <SkLine width="w-24" height="h-5" />
                <SkLine width="w-full" height="h-3" className="mt-3" />
              </SkBlock>
            </div>
          </div>
        </>
      )}
      {variant === "table" && (
        <>
          <div className="mb-6 flex items-center justify-between">
            <div className="space-y-2">
              <SkLine width="w-40" height="h-6" />
              <SkLine width="w-64" />
            </div>
            <SkLine width="w-32" height="h-8" className="rounded-md" />
          </div>
          <SkBlock>
            <div className="flex flex-wrap gap-2 border-b border-subtle p-3">
              {[0, 1, 2, 3].map((i) => (
                <SkLine key={i} width="w-28" height="h-8" className="rounded-md" />
              ))}
            </div>
            <div className="divide-y divide-subtle/60">
              {[0, 1, 2, 3, 4, 5, 6, 7].map((i) => (
                <div key={i} className="flex items-center gap-3 px-5 py-3">
                  <SkLine width="w-20" />
                  <SkLine width="flex-1" />
                  <SkLine width="w-20" />
                  <SkLine width="w-20" />
                  <SkLine width="w-16" />
                </div>
              ))}
            </div>
          </SkBlock>
        </>
      )}
      {variant === "narrative" && (
        <div className="space-y-5">
          <SkBlock className="px-5 py-4">
            <SkLine width="w-72" height="h-5" />
            <SkLine width="w-full" height="h-3" className="mt-2" />
            <SkLine width="w-2/3" height="h-3" className="mt-2" />
          </SkBlock>
          <SkBlock className="px-6 py-6 md:px-10 md:py-8">
            <SkLine width="w-3/4" height="h-7" />
            <div className="mt-4 space-y-2">
              <SkLine width="w-full" />
              <SkLine width="w-11/12" />
              <SkLine width="w-4/5" />
            </div>
            <SkLine width="w-1/2" height="h-6" className="mt-6" />
            <div className="mt-3 space-y-2">
              <SkLine width="w-full" />
              <SkLine width="w-10/12" />
              <SkLine width="w-9/12" />
            </div>
          </SkBlock>
        </div>
      )}
      {variant === "chat" && (
        <div className="grid grid-cols-1 gap-4 lg:grid-cols-[260px_1fr]">
          <SkBlock className="hidden lg:block p-3">
            {[0, 1, 2, 3].map((i) => (
              <SkLine key={i} width="w-full" height="h-10" className="mb-2 rounded-md" />
            ))}
          </SkBlock>
          <SkBlock className="p-5">
            <div className="space-y-4">
              {[0, 1, 2].map((i) => (
                <div key={i} className="space-y-1">
                  <SkLine width="w-20" height="h-3" />
                  <SkLine width="w-3/4" />
                  <SkLine width="w-2/3" />
                </div>
              ))}
            </div>
            <SkLine width="w-full" height="h-12" className="mt-6 rounded-md" />
          </SkBlock>
        </div>
      )}
      {variant === "coa" && (
        <>
          <div className="mb-6 flex items-center justify-between">
            <SkLine width="w-40" height="h-8" className="rounded-md" />
            <SkLine width="w-32" height="h-8" className="rounded-md" />
          </div>
          <div className="space-y-4">
            {[0, 1, 2].map((g) => (
              <SkBlock key={g}>
                <div className="border-b border-subtle px-5 py-3">
                  <SkLine width="w-32" height="h-4" />
                </div>
                <div className="divide-y divide-subtle/60">
                  {[0, 1, 2, 3, 4].map((i) => (
                    <div key={i} className="flex items-center gap-3 px-5 py-2.5">
                      <SkLine width="w-14" />
                      <SkLine width="flex-1" />
                      <SkLine width="w-20" />
                    </div>
                  ))}
                </div>
              </SkBlock>
            ))}
          </div>
        </>
      )}
      {variant === "reports" && (
        <>
          <div className="mb-6 flex items-center justify-between">
            <div className="space-y-2">
              <SkLine width="w-40" height="h-6" />
              <SkLine width="w-72" />
            </div>
          </div>
          <div className="mb-6 flex flex-wrap gap-2">
            {[0, 1, 2, 3].map((i) => (
              <SkLine key={i} width="w-24" height="h-8" className="rounded-md" />
            ))}
          </div>
          <SkBlock>
            <div className="space-y-2 p-5">
              {[0, 1, 2, 3, 4, 5, 6].map((i) => (
                <SkLine key={i} width="w-full" />
              ))}
            </div>
          </SkBlock>
        </>
      )}
      {variant === "default" && (
        <>
          <div className="mb-6 space-y-2">
            <SkLine width="w-48" height="h-6" />
            <SkLine width="w-72" />
          </div>
          <SkBlock className="p-6">
            <div className="space-y-3">
              {[0, 1, 2, 3, 4].map((i) => (
                <SkLine key={i} width={i % 2 === 0 ? "w-full" : "w-3/4"} />
              ))}
            </div>
          </SkBlock>
        </>
      )}
    </div>
  );
}
