import { AppShell } from "@/components/app-shell";
import { EntitySelector } from "@/components/entity-selector";
import { PageShell, SectionHeader } from "@/components/primitives";
import { getActiveContext } from "@/lib/active-context";
import { CoaEditor } from "./coa-editor";

export default async function CoaPage() {
  const ctx = await getActiveContext();
  const workspace = ctx.activeWorkspace;
  const canConsolidate = workspace
    ? ["owner", "admin"].includes(workspace.role)
    : false;

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
          activeEntityId={ctx.activeEntity?.id ?? ctx.entities[0]?.id ?? ""}
        />
      </PageShell>
    </AppShell>
  );
}
