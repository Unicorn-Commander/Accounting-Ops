import Link from "next/link";
import { ArrowRight, Activity } from "lucide-react";
import { Card } from "@/components/primitives";
import type { AIActivityResponse } from "@/lib/ai-activity";

export function scopedActivityRows(activity: AIActivityResponse | null, workspaceId: string, entityId?: string) {
  if (!activity || activity.rows.some(row => row.workspace_id !== workspaceId ||
      (entityId !== undefined && row.entity_id !== entityId))) return null;
  return activity.rows;
}

/** Read-only summary of the same scoped audit feed as the full activity page. */
export function AgentActivitySection({ activity, workspaceId, entityId, limit = 6 }: {
  activity: AIActivityResponse | null; workspaceId: string; entityId?: string; limit?: number;
}) {
  const rows = scopedActivityRows(activity, workspaceId, entityId);
  const events = rows?.slice(0, limit) ?? [];
  return (
    <Card padded={false}>
      <div className="flex flex-col gap-3 border-b border-subtle px-5 py-4 md:flex-row md:items-center md:justify-between md:px-6">
        <div className="flex items-start gap-2.5">
          <Activity size={16} className="mt-0.5 shrink-0 text-agent-500" />
          <div>
            <h2 className="text-base font-semibold">Agent activity</h2>
            <p className="mt-0.5 text-xs text-ink-tertiary">Recent recorded actions for the selected workspace and entity scope.</p>
          </div>
        </div>
        <Link href="/agent-activity" className="inline-flex shrink-0 items-center gap-1 self-start text-xs text-ink-secondary hover:text-ink-primary md:self-auto">
          View all <ArrowRight size={13} />
        </Link>
      </div>
      {rows === null ? (
        <p role="status" className="px-6 py-8 text-sm text-ink-tertiary">Activity is unavailable for this scope. Refresh or open the activity log to try again.</p>
      ) : events.length === 0 ? (
        <p className="px-6 py-8 text-sm text-ink-tertiary">No recorded agent activity for this scope yet.</p>
      ) : (
        <ul className="divide-y divide-subtle/60">
          {events.map(event => <li key={event.id} className="min-w-0 space-y-2 px-5 py-3 md:px-6">
            <div className="flex flex-wrap items-baseline gap-2">
              <span className="text-sm text-ink-primary">{event.tool_name === "post_journal_entry" && event.outcome !== "posted" ? "Journal entry request" : event.action_label || event.tool_name}</span>
              <span className="break-words font-mono text-xs text-ink-tertiary">{event.outcome}</span>
            </div>
            {event.reasoning_trace && <p className="break-words text-xs text-ink-secondary">{event.reasoning_trace}</p>}
            <p className="flex flex-wrap gap-2 text-xs text-ink-tertiary">
              <span>{event.entity_display_name || "Selected entity"}</span>
              <span className="break-all">{event.agent_id}</span>
              <time dateTime={event.created_at}>{Number.isNaN(Date.parse(event.created_at)) ? "Time unavailable" : new Date(event.created_at).toISOString().replace("T", " ").replace(".000Z", " UTC")}</time>
            </p>
          </li>)}
        </ul>
      )}
      {rows !== null && <p className="border-t border-subtle px-5 py-3 text-xs text-ink-tertiary md:px-6">Showing {events.length} recent recorded actions. Open the activity log for more.</p>}
    </Card>
  );
}
