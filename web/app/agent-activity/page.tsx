// /agent-activity — full-page AI activity timeline.
//
// Wired to live decision_audit + tax_review_queue (agent submissions) via
// fetchAIActivity. Replaces the previous ACTIVITY_EVENTS mock + "Preview"
// banner per UX audit issue I16.
//
// The richer in-page interactions (filter / live poll / undo / challenge)
// live in the AIActivityTimeline component, shared with /open-items
// section 7 + /entities/{eid}/activity.

import Link from "next/link";
import { AppShell } from "@/components/app-shell";
import { AIActivityTimeline } from "@/components/ai-activity-timeline";
import { Card, PageShell, SectionHeader } from "@/components/primitives";
import { fetchAIActivity } from "@/lib/ai-activity";
import { getActiveContext } from "@/lib/active-context";

export const dynamic = "force-dynamic";

export default async function AgentActivityPage() {
  const ctx = await getActiveContext();
  const workspace = ctx.activeWorkspace;

  if (!workspace) {
    return (
      <AppShell>
        <PageShell>
          <SectionHeader
            title="Activity log"
            subtitle="Every decision your agents make, with reasoning. Append-only. Tamper-evident."
          />
          <Card>
            <p className="text-sm text-ink-secondary">
              No workspace selected. Visit{" "}
              <Link
                href="/dashboard"
                className="text-brand-400 underline-offset-2 hover:underline"
              >
                Dashboard
              </Link>{" "}
              to pick one.
            </p>
          </Card>
        </PageShell>
      </AppShell>
    );
  }

  // Resolve entity scope from cookie/default. The endpoint also accepts
  // entity_id query parameter for further filtering — surfaced inside the
  // timeline's filter bar.
  const entityId = ctx.isConsolidated
    ? undefined
    : ctx.activeEntity?.id ?? undefined;

  const aiActivity = await fetchAIActivity(workspace.id, {
    entityId,
    limit: 200
  });

  const scopeLabel = ctx.isConsolidated
    ? "All entities (consolidated)"
    : ctx.activeEntity?.display_name ?? "(no entity)";

  return (
    <AppShell>
      <PageShell>
        <SectionHeader
          title="Activity log"
          subtitle={`Every decision your agents make, with reasoning. Append-only. Tamper-evident. · ${scopeLabel}`}
        />

        <AIActivityTimeline
          workspaceId={workspace.id}
          initial={aiActivity}
          entityId={entityId}
          emptyMsg={
            aiActivity === null
              ? "Activity log unavailable — backend returned no data. Try refreshing in a moment."
              : "No AI agent actions yet for the selected scope. Once an agent drafts a memo, posts a JE, or runs a specialist, you'll see it here."
          }
        />

        <p className="mt-4 text-[11px] text-ink-tertiary">
          Sourced from <code className="text-ink-secondary">decision_audit</code> +
          agent-submitted <code className="text-ink-secondary">tax_review_queue</code>{" "}
          rows. Append-only. Every undo / challenge writes its own audit row.
        </p>
      </PageShell>
    </AppShell>
  );
}
