import { Filter } from "lucide-react";
import { AppShell } from "@/components/app-shell";
import { Card, PageShell, SectionHeader } from "@/components/primitives";
import { ActivityEventList, ActivitySummaryStrip } from "@/components/agent-activity-feed";
import { ACTIVITY_EVENTS, summarizeActivity } from "@/lib/agent-activity";

export default function AgentActivityPage() {
  const summary = summarizeActivity(ACTIVITY_EVENTS);

  return (
    <AppShell>
      <PageShell>
        <SectionHeader
          title="Activity log"
          subtitle="Every decision your agents make, with reasoning. Append-only. Tamper-evident."
          action={
            <div className="flex items-center gap-2">
              <span className="inline-flex items-center gap-1 rounded-full border border-pending/40 bg-pending/[0.08] px-2 py-0.5 text-[10px] font-medium uppercase tracking-wider text-pending">
                Preview
              </span>
              <button className="inline-flex items-center gap-1.5 rounded-md border border-default bg-surface px-3 py-1.5 text-xs text-ink-secondary hover:bg-elevated focus:outline-none focus-visible:ring-2 focus-visible:ring-brand-500 focus-visible:ring-offset-2 focus-visible:ring-offset-canvas">
                <Filter size={13} /> Filters
              </button>
            </div>
          }
        />

        {/* Preview banner — audit quick-win #6 + issue 16: the events below
            are seeded sample data, not the real SSE stream. */}
        <div className="mb-4 rounded-lg border border-pending/40 bg-pending/[0.04] px-4 py-3 text-xs text-pending">
          <strong className="font-semibold">Preview:</strong> the events below
          are sample data showing the planned shape of the activity log. Real
          AI activity for your workspace is available on{" "}
          <a href="/open-items#ai-activity" className="underline">
            /open-items
          </a>{" "}
          in the &ldquo;Recent AI activity&rdquo; section.
        </div>

        <Card padded={false}>
          <div className="flex flex-wrap items-center gap-3 border-b border-subtle px-5 py-3">
            <ActivitySummaryStrip summary={summary} />
            <span className="ml-auto text-[11px] text-ink-tertiary">Sample data · last 60 minutes</span>
          </div>

          <ActivityEventList events={ACTIVITY_EVENTS} />

          <div className="flex items-center justify-between border-t border-subtle px-5 py-3 text-[11px] text-ink-tertiary">
            <span>Showing {ACTIVITY_EVENTS.length} sample events</span>
            <span>Phase 1 wires real SSE stream from FastAPI</span>
          </div>
        </Card>
      </PageShell>
    </AppShell>
  );
}
