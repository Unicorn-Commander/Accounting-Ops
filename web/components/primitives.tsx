"use client";

import { useEffect, useRef, useState } from "react";
import { cn } from "@/lib/cn";
import {
  AlertCircle,
  AlertTriangle,
  Inbox,
  Lock,
  Plug,
  Search,
} from "lucide-react";

// ---------------------------------------------------------------------------
// Card — gradient + glass-edge + shadow; lift variant on hover
// ---------------------------------------------------------------------------
export function Card({
  children,
  className,
  padded = true,
  lift = false,
}: {
  children: React.ReactNode;
  className?: string;
  padded?: boolean;
  lift?: boolean;
}) {
  return (
    <div
      className={cn(
        "card-depth",
        lift && "lift",
        padded && "p-4 md:p-6",
        className
      )}
    >
      {children}
    </div>
  );
}

// ---------------------------------------------------------------------------
// Sparkline — hand-rolled SVG with gradient area fill + line + end dot
// ---------------------------------------------------------------------------
export function Sparkline({
  data,
  width = 90,
  height = 32,
  color,
}: {
  data: number[];
  width?: number;
  height?: number;
  color: string;
}) {
  const n = data.length;
  if (n === 0) return null;
  const min = Math.min(...data);
  const max = Math.max(...data);
  const range = max - min || 1;
  const pad = 1;
  const X = (i: number) => (i / (n - 1)) * width;
  const Y = (v: number) => height - pad - ((v - min) / range) * (height - pad * 2 - 5);

  function smoothPath(pts: [number, number][]): string {
    let d = `M ${pts[0][0]},${pts[0][1]}`;
    for (let i = 0; i < pts.length - 1; i++) {
      const a = pts[i - 1] || pts[i];
      const b = pts[i];
      const c = pts[i + 1];
      const e = pts[i + 2] || c;
      d += ` C ${b[0] + (c[0] - a[0]) / 6},${b[1] + (c[1] - a[1]) / 6} ${c[0] - (e[0] - b[0]) / 6},${c[1] - (e[1] - b[1]) / 6} ${c[0]},${c[1]}`;
    }
    return d;
  }

  const pts: [number, number][] = data.map((v, i) => [X(i), Y(v)]);
  const line = smoothPath(pts);
  const area = line + ` L ${width},${height} L 0,${height} Z`;
  const fillId = `sf-${Math.random().toString(36).slice(2, 8)}`;

  return (
    <svg
      className="spark"
      viewBox={`0 0 ${width} ${height}`}
      preserveAspectRatio="none"
      style={{ width, height }}
    >
      <defs>
        <linearGradient id={fillId} x1="0" y1="0" x2="0" y2="1">
          <stop offset="0%" stopColor={color} stopOpacity="0.28" />
          <stop offset="100%" stopColor={color} stopOpacity="0" />
        </linearGradient>
      </defs>
      <path d={area} fill={`url(#${fillId})`} />
      <path d={line} fill="none" stroke={color} strokeWidth="2" strokeLinecap="round" />
      <circle cx={X(n - 1)} cy={Y(data[n - 1])} r="2.4" fill={color} />
    </svg>
  );
}

// ---------------------------------------------------------------------------
// KpiCard — big tabular-nums value with count-up on mount, delta chip, sparkline
// ---------------------------------------------------------------------------
function useCountUp(target: number, duration = 900) {
  const ref = useRef<HTMLDivElement>(null);
  const animated = useRef(false);

  useEffect(() => {
    if (animated.current) return;
    animated.current = true;
    const node = ref.current;
    if (!node) return;

    const prefersReduced = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
    if (prefersReduced) {
      node.textContent = formatNumber(target);
      return;
    }

    const t0 = performance.now();
    function tick(t: number) {
      const k = Math.min(1, (t - t0) / duration);
      const ease = 1 - Math.pow(1 - k, 3);
      if (node) node.textContent = formatNumber(target * ease);
      if (k < 1) requestAnimationFrame(tick);
    }
    requestAnimationFrame(tick);
  }, [target, duration]);

  return ref;
}

function formatNumber(v: number): string {
  return v.toLocaleString("en-US", {
    minimumFractionDigits: 2,
    maximumFractionDigits: 2,
  });
}

export function KpiCard({
  label,
  value,
  delta,
  deltaTone = "ink",
  sparklineData,
  sparklineColor,
  prefix = "$",
  targetValue,
  onClick,
}: {
  label: string;
  value: string;
  delta?: string;
  deltaTone?: "credit" | "debit" | "pending" | "ink";
  sparklineData?: number[];
  sparklineColor?: string;
  prefix?: string;
  targetValue?: number;
  onClick?: () => void;
}) {
  const deltaMap: Record<string, string> = {
    credit: "text-credit",
    debit: "text-debit",
    pending: "text-pending",
    ink: "text-ink-tertiary",
  };

  const countUpRef = targetValue !== undefined ? useCountUp(targetValue) : undefined;

  return (
    <div className="card-depth kpi cursor-pointer" onClick={onClick}>
      <div className="lbl">{label}</div>
      <div className="v num" ref={countUpRef}>
        {countUpRef ? `${prefix}0.00` : value}
      </div>
      <div className="meta">
        {delta ? (
          <span className={cn("d", deltaTone === "credit" ? "up" : deltaTone === "debit" ? "down" : "flat")}>
            {deltaTone === "debit" ? "▼" : "▲"} {delta.replace(/^[+\-▲▼]/, "")}
          </span>
        ) : (
          <span />
        )}
        {sparklineData && sparklineColor ? (
          <Sparkline data={sparklineData} color={sparklineColor} />
        ) : null}
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// ConfidenceRing — conic-gradient ring with % label and "conf" caption
// ---------------------------------------------------------------------------
export function ConfidenceRing({
  value,
  size = 40,
  color,
}: {
  value: number;
  size?: number;
  color?: string;
}) {
  const p = Math.round(value);
  const c =
    color ??
    (p >= 90
      ? "var(--credit)"
      : p >= 70
        ? "var(--pending)"
        : "var(--debit)");
  const s = { "--p": p, "--c": c } as React.CSSProperties;
  return (
    <div
      className="ring"
      style={{ ...s, width: size, height: size }}
    >
      <span>{p}</span>
      <small>conf</small>
    </div>
  );
}

// ---------------------------------------------------------------------------
// StatusBadge — appr | await | block | think
// ---------------------------------------------------------------------------
const STATUS_MAP: Record<string, { label: string; cls: string }> = {
  appr: { label: "auto-approved", cls: "status appr" },
  await: { label: "needs you", cls: "status await" },
  block: { label: "blocked", cls: "status block" },
  think: { label: "thinking", cls: "status think" },
};

export function StatusBadge({
  variant,
}: {
  variant: "appr" | "await" | "block" | "think";
}) {
  const s = STATUS_MAP[variant];
  return <span className={s.cls}>{s.label}</span>;
}

// ---------------------------------------------------------------------------
// Odometer — animates a number old→new, cubic ease ~1.1s
// ---------------------------------------------------------------------------
export function Odometer({
  from,
  to,
  prefix = "$",
  duration = 1100,
}: {
  from: number;
  to: number;
  prefix?: string;
  duration?: number;
}) {
  const ref = useRef<HTMLSpanElement>(null);

  useEffect(() => {
    const node = ref.current;
    if (!node) return;

    const prefersReduced = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
    if (prefersReduced) {
      node.textContent = prefix + formatNumber(to);
      return;
    }

    const t0 = performance.now();
    function tick(t: number) {
      const k = Math.min(1, (t - t0) / duration);
      const ease = 1 - Math.pow(1 - k, 3);
      const current = from + (to - from) * ease;
      if (node) node.textContent = prefix + formatNumber(current);
      if (k < 1) requestAnimationFrame(tick);
    }
    requestAnimationFrame(tick);
  }, [from, to, prefix, duration]);

  return <span ref={ref} className="num">{prefix}{formatNumber(from)}</span>;
}

// ---------------------------------------------------------------------------
// Original primitives below — kept as-is for backward compat
// ---------------------------------------------------------------------------

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

export function PageShell({ children }: { children: React.ReactNode }) {
  return (
    <div className="relative z-10 mx-auto w-full max-w-7xl px-4 py-5 md:px-6 md:py-8">
      {children}
    </div>
  );
}

// ----------------------------------------------------------------------------
// Empty / Error / Skeleton primitives (unchanged)
// ----------------------------------------------------------------------------

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
// RouteSkeleton
// ----------------------------------------------------------------------------

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
