// AI agreement (review-vs-redo) card — a compact, read-only accuracy KPI for
// the per-workspace dashboard.
//
// Surfaces the metric from GET /api/workspaces/{wid}/metrics/ai-agreement
// (app/api/metrics.py): how often the human AGREED with the AI's call on the
// approval queue. High agreement => the human is reviewing AI work; low
// agreement => they're redoing it. This is the core "10x CPA" signal.
//
// Presentational Server Component. The page hands it the already-fetched
// metric (or null). On null it renders a soft "unavailable" state — it never
// crashes the dashboard. When there are no decided approvals yet
// (agreement_rate === null) it shows a "no decisions reviewed yet" empty state,
// NOT a misleading 0%.

import { Sparkles } from "lucide-react";
import { Card } from "@/components/primitives";
import { cn } from "@/lib/cn";
import {
  formatAgreementPct,
  type AiAgreementResponse
} from "@/lib/ai-agreement";

// One-line explanation of the metric, surfaced as a subtitle + on-hover title.
const METRIC_TOOLTIP =
  "How often you agreed with the AI's call — high = review, low = redo.";

// Tone the headline by how good the agreement is. These are deliberately
// generous: anything <50% means you're redoing more than half the AI's calls.
function rateTone(rate: number): string {
  if (rate >= 0.85) return "text-credit";
  if (rate >= 0.5) return "text-pending";
  return "text-debit";
}

function CardFrame({ children }: { children: React.ReactNode }) {
  return (
    <Card padded={false} className="overflow-hidden">
      <div className="flex items-center gap-2 border-b border-subtle px-5 py-3">
        <Sparkles size={12} className="text-agent-500" />
        <h2 className="text-sm font-semibold">AI agreement</h2>
        <span className="ml-auto rounded-full bg-white/5 px-2 py-0.5 text-[10px] font-medium uppercase tracking-wider text-ink-tertiary">
          Review vs redo
        </span>
      </div>
      {children}
    </Card>
  );
}

export function AiAgreementCard({
  metric
}: {
  // null => the fetch failed; render the soft "unavailable" state.
  metric: AiAgreementResponse | null;
}) {
  // Soft-fail: the endpoint was unreachable. Never crash the dashboard.
  if (!metric) {
    return (
      <CardFrame>
        <div className="px-5 py-6 text-center">
          <p className="text-xs text-ink-tertiary">
            Agreement metric unavailable right now.
          </p>
        </div>
      </CardFrame>
    );
  }

  const last90 = metric.last_90d;
  const allTime = metric.all_time;
  const headlinePct = formatAgreementPct(last90.agreement_rate);
  const allTimePct = formatAgreementPct(allTime.agreement_rate);
  // Deferrals = approvals the AI explicitly handed to the human (excluded from
  // the rate by design). Surfaced as "N deferred to you" for context.
  const deferred = last90.flagged_for_review;

  // Empty state: no decided (non-flag) approvals in the last 90 days. Show
  // guidance, not 0%. Fall back to all-time so a workspace with older history
  // still shows something meaningful before we declare it empty.
  if (headlinePct === null) {
    // If there IS all-time history, lead with that rather than the empty state.
    if (allTimePct !== null) {
      return (
        <CardFrame>
          <div className="px-5 py-4">
            <p
              className="text-[10px] font-medium uppercase tracking-wider text-ink-tertiary"
              title={METRIC_TOOLTIP}
            >
              Agreement (all time)
            </p>
            <p
              className={cn(
                "mt-1.5 font-mono text-numeric-lg tabular-nums",
                rateTone(allTime.agreement_rate ?? 0)
              )}
            >
              {allTimePct}
            </p>
            <p className="mt-1 text-xs text-ink-secondary">
              {allTime.agreed} of {allTime.total_decided} agreed · no decisions
              in the last 90 days
            </p>
            <p className="mt-2 text-[11px] leading-relaxed text-ink-tertiary">
              {METRIC_TOOLTIP}
            </p>
          </div>
        </CardFrame>
      );
    }
    return (
      <CardFrame>
        <div className="px-5 py-6 text-center">
          <p className="text-sm text-ink-secondary">No decisions reviewed yet</p>
          <p className="mx-auto mt-1 max-w-xs text-[11px] leading-relaxed text-ink-tertiary">
            Once you approve or reject the AI&apos;s recommendations in your
            queue, this shows how often you agreed with its call.
          </p>
          {deferred > 0 ? (
            <p className="mt-2 text-[11px] text-ink-tertiary">
              {deferred} {deferred === 1 ? "item" : "items"} the AI deferred to
              you
            </p>
          ) : null}
        </div>
      </CardFrame>
    );
  }

  return (
    <CardFrame>
      <div className="px-5 py-4">
        <p
          className="text-[10px] font-medium uppercase tracking-wider text-ink-tertiary"
          title={METRIC_TOOLTIP}
        >
          Agreement (last 90 days)
        </p>
        <p
          className={cn(
            "mt-1.5 font-mono text-numeric-lg tabular-nums",
            rateTone(last90.agreement_rate ?? 0)
          )}
        >
          {headlinePct}
        </p>
        <p className="mt-1 text-xs text-ink-secondary">
          {last90.agreed} of {last90.total_decided} agreed
        </p>

        {/* Secondary line: all-time rate + deferral count for context. */}
        <div className="mt-3 flex flex-wrap items-center gap-x-3 gap-y-1 border-t border-subtle/60 pt-2.5 text-[11px] text-ink-tertiary">
          <span>
            All time:{" "}
            <span className="font-mono tabular-nums text-ink-secondary">
              {allTimePct ?? "—"}
            </span>
          </span>
          {deferred > 0 ? (
            <span className="before:mr-3 before:text-ink-disabled before:content-['·']">
              {deferred} deferred to you
            </span>
          ) : null}
        </div>

        <p className="mt-2 text-[11px] leading-relaxed text-ink-tertiary">
          {METRIC_TOOLTIP}
        </p>
      </div>
    </CardFrame>
  );
}
