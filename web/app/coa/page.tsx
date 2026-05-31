import { AppShell } from "@/components/app-shell";
import { EntitySelector } from "@/components/entity-selector";
import { PageShell, SectionHeader } from "@/components/primitives";
import { getActiveContext } from "@/lib/active-context";
import { fetchChartOfAccounts } from "@/lib/coa";
import { CoaEditor } from "./coa-editor";

export const dynamic = "force-dynamic";

export default async function CoaPage({
  searchParams,
}: {
  searchParams?: Promise<{ entity?: string }>;
}) {
  const ctx = await getActiveContext();
  const workspace = ctx.activeWorkspace;
  const canConsolidate = workspace
    ? ["owner", "admin"].includes(workspace.role)
    : false;

  // Entity resolution: ?entity= overrides cookie; cookie overrides default
  const params = (await searchParams) ?? {};
  const requestedEntityId = params.entity;
  const activeEntityId =
    (requestedEntityId &&
      ctx.entities.find((e) => e.id === requestedEntityId)?.id) ??
    ctx.activeEntity?.id ??
    ctx.entities[0]?.id ??
    "";

  // Server-side prefetch of the COA list — bypasses the broken client-side
  // proxy entirely.  See web/lib/coa.ts for the why.
  const initial =
    workspace && activeEntityId
      ? await fetchChartOfAccounts(workspace.id, activeEntityId)
      : { entity_id: "", accounts: [] };

  return (
    <AppShell>
      <PageShell>
        <SectionHeader
          title="Chart of Accounts"
          subtitle={
            workspace
              ? `Manage the chart of accounts for ${workspace.display_name}.`
              : "Create, rename, reclassify, or deactivate accounts."
          }
          action={
            <EntitySelector
              entities={ctx.entities}
              activeEntityId={
                ctx.isConsolidated ? "consolidated" : ctx.activeEntity?.id
              }
              canConsolidate={canConsolidate && ctx.entities.length > 1}
            />
          }
        />
        <CoaEditor
          workspaceId={workspace?.id ?? ""}
          entities={ctx.entities}
          activeEntityId={activeEntityId}
          initialAccounts={initial.accounts}
        />
      </PageShell>
    </AppShell>
  );
}
