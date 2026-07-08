import { AlertCircle, CheckCircle2, MoreHorizontal, RotateCw, ShieldAlert, XCircle } from "lucide-react";
import { MaterialityBadge } from "@/components/primitives";
import { cn } from "@/lib/cn";
import type { AgentEvent, ActivitySummary, Verdict } from "@/lib/agent-activity";

const VERDICT_STYLES: Record<Verdict, { bg: string; border: string; text: string; icon: React.ReactNode }> = {
  APPROVE: {
    bg: "bg-credit/[0.03]",
    border: "border-l-credit/30",
    text: "text-credit",
    icon: <CheckCircle2 size={14} />
  },
  REJECT: {
    bg: "bg-debit/[0.05]",
    border: "border-l-debit/40",
    text: "text-debit",
    icon: <XCircle size={14} />
  },
  NEEDS_REVIEW: {
    bg: "bg-pending/[0.06]",
    border: "border-l-pending/60",
    text: "text-pending",
    icon: <AlertCircle size={14} />
  },
  BLOCKED: {
    bg: "bg-blocked/[0.06]",
    border: "border-l-blocked/60",
    text: "text-blocked",
    icon: <ShieldAlert size={14} />
  },
  THINKING: {
    bg: "bg-agent-500/[0.05]",
    border: "border-l-agent-500/50",
    text: "text-agent-500",
    icon: <RotateCw size={14} className="animate-spin" />
  },
  CANCELED: {
    bg: "",
    border: "border-l-ink-disabled/30",
    text: "text-ink-tertiary",
    icon: <MoreHorizontal size={14} />
  }
};

/** Live-status summary strip (thinking / awaiting / blocked / auto-approved). */
export function ActivitySummaryStrip({
  summary,
  autoApproved = 47,
  className
}: {
  summary: ActivitySummary;
  autoApproved?: number;
  className?: string;
}) {
  return (
    <div className={cn("flex flex-wrap items-center gap-3", className)}>
      <div className="flex items-center gap-2 text-xs">
        <span className="h-1.5 w-1.5 rounded-full bg-agent-500 animate-pulse-soft shadow-agent-glow" />
        <span className="font-mono text-ink-secondary">{summary.thinking} thinking</span>
      </div>
      <span className="text-ink-disabled">·</span>
      <div className="flex items-center gap-2 text-xs">
        <span className="h-1.5 w-1.5 rounded-full bg-pending" />
        <span className="font-mono text-ink-secondary">{summary.awaiting} awaiting approval</span>
      </div>
      <span className="text-ink-disabled">·</span>
      <div className="flex items-center gap-2 text-xs">
        <span className="h-1.5 w-1.5 rounded-full bg-blocked" />
        <span className="font-mono text-ink-secondary">{summary.blocked} blocked</span>
      </div>
      <span className="text-ink-disabled">·</span>
      <div className="flex items-center gap-2 text-xs">
        <span className="h-1.5 w-1.5 rounded-full bg-credit" />
        <span className="font-mono text-ink-secondary">{autoApproved} auto-approved today</span>
      </div>
    </div>
  );
}

/** A single agent decision row. Shared by the dashboard section + full page. */
export function ActivityEventRow({ event }: { event: AgentEvent }) {
  const style = VERDICT_STYLES[event.verdict];
  return (
    <li
      className={cn(
        "flex gap-3 border-l-2 px-4 py-3 transition hover:bg-white/[0.02] md:gap-4 md:px-5",
        style.bg,
        style.border
      )}
    >
      <div className="shrink-0 pt-0.5">
        <span className={style.text}>{style.icon}</span>
      </div>
      <div className="min-w-0 flex-1">
        <div className="flex flex-wrap items-baseline gap-x-3">
          <span className="shrink-0 font-mono text-xs text-ink-tertiary">{event.time}</span>
          <span className={cn("font-mono text-xs font-semibold", style.text)}>{event.agent}</span>
          <span className="font-mono text-xs text-ink-tertiary">·</span>
          <span className="text-sm text-ink-primary">{event.action}</span>
        </div>
        {event.reasoning && (
          <p className="mt-1 font-reasoning text-xs leading-relaxed text-ink-secondary">
            {event.reasoning}
          </p>
        )}
        <div className="mt-1.5 flex flex-wrap items-center gap-2 text-[11px]">
          <span className="font-mono text-ink-tertiary">{event.tenant}</span>
          {event.materiality && <MaterialityBadge level={event.materiality} />}
          {event.amount && <span className="font-mono text-ink-secondary">{event.amount}</span>}
          <span className={cn("ml-auto text-[10px] uppercase tracking-wider", style.text)}>
            {event.verdict.replace("_", " ")}
          </span>
        </div>
      </div>
    </li>
  );
}

/** A scrollable list of agent decision rows. */
export function ActivityEventList({
  events,
  className
}: {
  events: AgentEvent[];
  className?: string;
}) {
  return (
    <ul className={cn("divide-y divide-subtle/60", className)}>
      {events.map((event, i) => (
        <ActivityEventRow key={`${event.time}-${i}`} event={event} />
      ))}
    </ul>
  );
}
