// /entities/{eid}/activity — per-entity AI activity drill-down.
//
// Same timeline component as the section on /open-items, but scoped to a
// single entity and lifted to its own page for deeper inspection.

import Link from "next/link";
import { AppShell } from "@/components/app-shell";
import { AIActivityTimeline } from "@/components/ai-activity-timeline";
import { Card, PageShell, SectionHeader } from "@/components/primitives";
import { fetchAIActivity } from "@/lib/ai-activity";
import { getActiveContext } from "@/lib/active-context";

export const dynamic = "force-dynamic";

export default async function EntityActivityPage({
  params
}: {
  params: Promise<{ entityId: string }>;
}) {
  const { entityId } = await params;
  const ctx = await getActiveContext();
  const workspace = ctx.activeWorkspace;

  if (!workspace) {
    return (
      <AppShell>
        <PageShell>
          <SectionHeader
            title="Entity AI activity"
            subtitle="No workspace selected."
          />
          <Card>
            <p className="text-sm text-ink-secondary">
              Visit{" "}
              <Link
                href="/dashboard"
                className="text-brand-400 underline-offset-2 hover:underline"
              >
                Dashboard
              </Link>{" "}
              to pick a workspace.
            </p>
          </Card>
        </PageShell>
      </AppShell>
    );
  }

  const aiActivity = await fetchAIActivity(workspace.id, {
    entityId,
    limit: 200
  });

  const entityName =
    ctx.entities.find((e) => e.id === entityId)?.display_name ?? entityId;

  return (
    <AppShell>
      <PageShell>
        <SectionHeader
          title={`AI activity — ${entityName}`}
          subtitle="Chronological feed of every AI agent action against this entity. Click any row for the reasoning trace and (when reversible) the undo button."
        />
        <AIActivityTimeline
          workspaceId={workspace.id}
          initial={aiActivity}
          entityId={entityId}
          emptyMsg="No AI activity recorded for this entity yet."
        />
      </PageShell>
    </AppShell>
  );
}
