import Link from "next/link";
import { ArrowRight, Activity } from "lucide-react";
import { Card } from "@/components/primitives";
import { ActivityEventList, ActivitySummaryStrip } from "@/components/agent-activity-feed";
import { ACTIVITY_EVENTS, summarizeActivity } from "@/lib/agent-activity";

// First-class inline dashboard section for agent activity (replaces the old
// floating "Agent activity" ticker popout). Lists the agent's most recent
// actions; the full append-only log lives at /agent-activity.
//
// Data source is the shared `ACTIVITY_EVENTS` module — the same one the full
// page reads — so wiring the live SSE stream later updates both at once.

export function AgentActivitySection({ limit = 6 }: { limit?: number }) {
  const events = ACTIVITY_EVENTS.slice(0, limit);
  const summary = summarizeActivity(ACTIVITY_EVENTS);

  return (
    <Card padded={false}>
      <div className="flex flex-col gap-3 border-b border-subtle px-5 py-4 md:flex-row md:items-center md:justify-between md:px-6">
        <div className="flex items-start gap-2.5">
          <Activity size={16} className="mt-0.5 shrink-0 text-agent-500" />
          <div>
            <h2 className="text-base font-semibold">Agent activity</h2>
            <p className="mt-0.5 text-xs text-ink-tertiary">
              Recent decisions across your entities · append-only · tamper-evident
            </p>
          </div>
        </div>
        <Link
          href="/agent-activity"
          className="inline-flex shrink-0 items-center gap-1 self-start text-xs text-ink-secondary hover:text-ink-primary md:self-auto"
        >
          View all <ArrowRight size={13} />
        </Link>
      </div>

      <div className="border-b border-subtle px-5 py-3 md:px-6">
        <ActivitySummaryStrip summary={summary} />
      </div>

      {events.length > 0 ? (
        <ActivityEventList events={events} />
      ) : (
        <div className="px-6 py-12 text-center text-sm text-ink-tertiary">
          <p>No agent activity yet.</p>
          <p className="mt-2 text-xs">
            The first decisions will land when the Bookkeeper agent begins categorizing your
            live transactions.
          </p>
        </div>
      )}

      <div className="flex items-center justify-between border-t border-subtle px-5 py-3 text-[11px] text-ink-tertiary md:px-6">
        <span>
          Showing {events.length} of {ACTIVITY_EVENTS.length} recent events
        </span>
        <span>Phase 1 wires real SSE stream from FastAPI</span>
      </div>
    </Card>
  );
}
