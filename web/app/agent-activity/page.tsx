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
          title="Agent activity"
          subtitle="Every decision your agents make, in real time, with reasoning. Append-only. Tamper-evident."
          action={
            <button className="inline-flex items-center gap-1.5 rounded-md border border-default bg-surface px-3 py-1.5 text-xs text-ink-secondary hover:bg-elevated">
              <Filter size={13} /> Filters
            </button>
          }
        />

        <Card padded={false}>
          <div className="flex flex-wrap items-center gap-3 border-b border-subtle px-5 py-3">
            <ActivitySummaryStrip summary={summary} />
            <span className="ml-auto text-[11px] text-ink-tertiary">Last 60 minutes · SSE live</span>
          </div>

          <ActivityEventList events={ACTIVITY_EVENTS} />

          <div className="flex items-center justify-between border-t border-subtle px-5 py-3 text-[11px] text-ink-tertiary">
            <span>Showing {ACTIVITY_EVENTS.length} of 247 events today</span>
            <span>Phase 1 wires real SSE stream from FastAPI</span>
          </div>
        </Card>
      </PageShell>
    </AppShell>
  );
}
