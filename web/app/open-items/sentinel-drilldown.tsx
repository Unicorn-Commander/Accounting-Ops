"use client";

// Sentinel summary tile — demoted from the primary /open-items surface to a
// secondary block, per audit issue C1. Renders compact totals + a link to
// the full Sentinel dashboard at /agent-activity (and /settings/sentinel).
//
// Renders nothing if Sentinel returned no data (e.g. backend unhealthy or
// not configured for this workspace) so we don't leak an empty card.

import Link from "next/link";
import { ChevronRight, Shield } from "lucide-react";
import { Card } from "@/components/primitives";

export function SentinelDrilldown({
  totals,
  hasData
}: {
  totals: {
    open: number;
    cycles: number;
    actions: number;
    scheduled: number;
  };
  hasData: boolean;
}) {
  if (!hasData) return null;

  return (
    <div className="mt-10">
      <div className="mb-2 flex items-center gap-2 text-[10px] font-medium uppercase tracking-wider text-ink-tertiary">
        <Shield size={12} />
        <span>Autopilot summary (last 7 days)</span>
      </div>
      <Card padded={false}>
        <div className="grid grid-cols-2 gap-px bg-subtle/40 sm:grid-cols-4">
          <Tile label="Open findings" value={totals.open} />
          <Tile label="Cycles" value={totals.cycles} />
          <Tile label="Actions taken" value={totals.actions} />
          <Tile label="Scheduled" value={totals.scheduled} />
        </div>
        <div className="flex flex-wrap items-center justify-between gap-2 border-t border-subtle/60 px-4 py-3 text-[11px] text-ink-tertiary">
          <span>
            Sentinel runs autonomously to spot deadlines, anomalies, and missing
            data. Owner-actionable items are surfaced above.
          </span>
          <div className="flex items-center gap-3">
            <Link
              href="/agent-activity"
              className="inline-flex items-center gap-1 text-brand-400 underline-offset-2 hover:underline focus:outline-none focus-visible:ring-2 focus-visible:ring-brand-500 focus-visible:ring-offset-2 focus-visible:ring-offset-canvas rounded-sm"
            >
              View activity log
              <ChevronRight size={11} />
            </Link>
            <Link
              href="/settings/sentinel"
              className="inline-flex items-center gap-1 text-ink-secondary underline-offset-2 hover:text-ink-primary hover:underline focus:outline-none focus-visible:ring-2 focus-visible:ring-brand-500 focus-visible:ring-offset-2 focus-visible:ring-offset-canvas rounded-sm"
            >
              Configure
              <ChevronRight size={11} />
            </Link>
          </div>
        </div>
      </Card>
    </div>
  );
}

function Tile({ label, value }: { label: string; value: number }) {
  return (
    <div className="bg-surface px-4 py-3">
      <p className="text-[10px] font-medium uppercase tracking-wider text-ink-tertiary">
        {label}
      </p>
      <p className="mt-1 font-mono text-xl tabular-nums">{value}</p>
    </div>
  );
}
