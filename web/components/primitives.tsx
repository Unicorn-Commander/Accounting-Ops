import { cn } from "@/lib/cn";

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
    <div className="rounded-xl border border-default bg-surface p-4 md:p-6 transition hover:border-strong">
      <p className="text-[10px] font-medium uppercase tracking-wider text-ink-tertiary">{label}</p>
      <p className="mt-2 md:mt-3 font-mono text-numeric-lg text-ink-primary break-all">{value}</p>
      {delta ? <p className={cn("mt-1 text-xs md:text-sm", deltaMap[deltaTone])}>{delta}</p> : null}
    </div>
  );
}

export function SectionHeader({
  title,
  subtitle,
  action
}: {
  title: string;
  subtitle?: string;
  action?: React.ReactNode;
}) {
  return (
    <div className="mb-5 md:mb-6 flex flex-col gap-3 sm:flex-row sm:flex-wrap sm:items-end sm:justify-between">
      <div>
        <h1 className="text-xl md:text-2xl font-semibold tracking-tight">{title}</h1>
        {subtitle ? <p className="mt-1 text-xs md:text-sm text-ink-secondary">{subtitle}</p> : null}
      </div>
      {action ? <div className="flex flex-wrap items-center gap-2">{action}</div> : null}
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
        "rounded-xl border border-default bg-surface",
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
