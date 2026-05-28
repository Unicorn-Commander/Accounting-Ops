import { AppShell } from "@/components/app-shell";
import { Card, PageShell, SectionHeader } from "@/components/primitives";
import { getActiveContext } from "@/lib/active-context";
import { getClosedPeriods } from "@/lib/reports-trial-balance";
import { PeriodCloseClient } from "./page-client";

export default async function PeriodClosePage() {
  const ctx = await getActiveContext();
  const workspace = ctx.activeWorkspace;
  const canAdmin = workspace
    ? ["owner", "admin"].includes(workspace.role)
    : false;

  const closedPeriods =
    workspace && canAdmin
      ? await Promise.all(
          ctx.entities.map(async (entity) => ({
            entityId: entity.id,
            periods:
              (await getClosedPeriods(workspace.id, entity.id))?.closed_periods ?? [],
          }))
        )
      : [];

  return (
    <AppShell>
      <PageShell>
        <SectionHeader
          title="Period Close"
          subtitle="Lock prior periods to refuse late postings."
        />

        {!workspace ? (
          <Card>
            <p className="text-sm text-ink-tertiary">No active workspace.</p>
          </Card>
        ) : !canAdmin ? (
          <Card>
            <p className="text-sm text-ink-tertiary">
              Admin access is required to close accounting periods.
            </p>
          </Card>
        ) : ctx.entities.length === 0 ? (
          <Card>
            <p className="text-sm text-ink-tertiary">
              No entities are available in this workspace.
            </p>
          </Card>
        ) : (
          <PeriodCloseClient
            workspaceId={workspace.id}
            entities={ctx.entities}
            initialClosedPeriods={closedPeriods}
          />
        )}
      </PageShell>
    </AppShell>
  );
}
