import Link from "next/link";
import { AppShell } from "@/components/app-shell";
import { EntitySelector } from "@/components/entity-selector";
import { Card, PageShell, SectionHeader } from "@/components/primitives";
import { getActiveContext } from "@/lib/active-context";
import { FormsHubClient } from "./forms-hub-client";

export const dynamic = "force-dynamic";

export default async function FormsHubPage() {
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
            title="Tax Forms"
            subtitle="Prepare any of these forms — filled, ready to review, sign, and file"
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

  // The catalog is workspace-level browsing — it does NOT need a concrete
  // entity. The EntitySelector is offered so the user can set their working
  // entity before they jump into a prepare screen (which does need one).
  return (
    <AppShell>
      <PageShell>
        <SectionHeader
          title="Tax Forms"
          subtitle="Prepare any of these forms — filled, ready to review, sign, and file"
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

        <FormsHubClient workspaceId={workspace.id} />
      </PageShell>
    </AppShell>
  );
}
