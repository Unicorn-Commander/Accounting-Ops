import Link from "next/link";
import { AppShell } from "@/components/app-shell";
import { EntitySelector } from "@/components/entity-selector";
import { Card, PageShell, SectionHeader } from "@/components/primitives";
import { getActiveContext } from "@/lib/active-context";
import { CpaExportClient } from "./cpa-export-client";

export const dynamic = "force-dynamic";

export default async function CpaExportPage() {
  const ctx = await getActiveContext();
  const workspace = ctx.activeWorkspace;
  const canConsolidate = workspace
    ? ["owner", "admin"].includes(workspace.role)
    : false;

  if (!workspace) {
    return (
      <AppShell>
        <PageShell>
          <SectionHeader
            title="CPA Export"
            subtitle="Download accounting data in CPA-ready formats"
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

  const entityId =
    ctx.isConsolidated || !ctx.activeEntity ? null : ctx.activeEntity.id;
  const entitySlug =
    ctx.isConsolidated || !ctx.activeEntity ? null : ctx.activeEntity.slug;
  const entityDisplayName = ctx.isConsolidated
    ? "All entities (consolidated)"
    : ctx.activeEntity?.display_name ?? "—";

  return (
    <AppShell>
      <PageShell>
        <SectionHeader
          title="CPA Export"
          subtitle="Download accounting data in CPA-ready formats"
          action={
            <EntitySelector
              entities={ctx.entities}
              activeEntityId={ctx.isConsolidated ? "consolidated" : ctx.activeEntity?.id}
              canConsolidate={canConsolidate}
            />
          }
        />

        <CpaExportClient
          workspaceId={workspace.id}
          workspaceSlug={workspace.slug}
          entityId={entityId}
          entitySlug={entitySlug}
          entityDisplayName={entityDisplayName}
        />
      </PageShell>
    </AppShell>
  );
}
