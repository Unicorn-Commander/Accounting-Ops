// /open-items — Aaron's Open Items dashboard.
//
// Server component: fetches the aggregated open-items payload from FastAPI
// and hands it to the OpenItemsView client component. The client component
// owns filter state (tax year / entity) and renders all six sections.

import Link from "next/link";
import { AppShell } from "@/components/app-shell";
import { EntitySelector } from "@/components/entity-selector";
import { Card, PageShell, SectionHeader } from "@/components/primitives";
import { fetchAIActivity } from "@/lib/ai-activity";
import { getActiveContext } from "@/lib/active-context";
import { fetchOpenItems } from "@/lib/open-items";
import { OpenItemsView } from "./open-items-view";

export const dynamic = "force-dynamic";

type SearchParams = Promise<{
  tax_year?: string;
}>;

export default async function OpenItemsPage({
  searchParams
}: {
  searchParams: SearchParams;
}) {
  const ctx = await getActiveContext();
  const workspace = ctx.activeWorkspace;
  const canConsolidate = workspace
    ? ["owner", "admin"].includes(workspace.role)
    : false;
  const { tax_year } = await searchParams;
  const taxYearFilter = tax_year ? parseInt(tax_year, 10) : undefined;

  if (!workspace) {
    return (
      <AppShell>
        <PageShell>
          <SectionHeader
            title="Open Items"
            subtitle="Action list — decisions, documents, and time-sensitive work waiting on you."
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
              to provision or pick one.
            </p>
          </Card>
        </PageShell>
      </AppShell>
    );
  }

  // Default entity scope: if the user picked one, use it; if they're on
  // "consolidated" leave it open so the dashboard rolls all entities up.
  const entityFilter =
    !ctx.isConsolidated && ctx.activeEntity?.id
      ? ctx.activeEntity.id
      : undefined;

  const [data, aiActivity] = await Promise.all([
    fetchOpenItems(workspace.id, {
      entityId: entityFilter,
      taxYear: taxYearFilter
    }),
    fetchAIActivity(workspace.id, {
      entityId: entityFilter,
      limit: 50
    })
  ]);

  const subtitle = ctx.activeWorkspace
    ? `${ctx.activeWorkspace.display_name} · viewing ${
        ctx.isConsolidated
          ? "all entities (consolidated)"
          : ctx.activeEntity?.display_name ?? "—"
      }${taxYearFilter ? ` · TY ${taxYearFilter}` : " · all years"}`
    : "Action list";

  return (
    <AppShell>
      <PageShell>
        <SectionHeader
          title="My Open Items"
          subtitle={subtitle}
          action={
            <EntitySelector
              entities={ctx.entities}
              activeEntityId={
                ctx.isConsolidated ? "consolidated" : ctx.activeEntity?.id
              }
              canConsolidate={canConsolidate}
            />
          }
        />

        {data ? (
          <OpenItemsView
            data={data}
            workspaceId={workspace.id}
            currentTaxYear={taxYearFilter ?? null}
            aiActivity={aiActivity}
            entityId={entityFilter}
          />
        ) : (
          <Card>
            <p className="text-sm text-ink-secondary">
              Could not load open items. The aggregator endpoint returned no
              data — check that the FastAPI service is healthy and you have
              viewer access to this workspace.
            </p>
          </Card>
        )}
      </PageShell>
    </AppShell>
  );
}
