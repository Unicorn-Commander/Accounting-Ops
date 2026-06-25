import Link from "next/link";
import { AppShell } from "@/components/app-shell";
import { EntitySelector } from "@/components/entity-selector";
import { Card, PageShell, SectionHeader } from "@/components/primitives";
import { getActiveContext } from "@/lib/active-context";
import { W4pClient } from "./w4p-client";

export const dynamic = "force-dynamic";

export default async function W4pPage() {
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
            title="Form W-4P — Pension Withholding"
            subtitle="Review, self-certify, and download your filled W-4P"
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

  // A W-4P belongs to a single person/entity, so this screen needs ONE concrete
  // entity. When the user is in the consolidated view (or has no entity), ask
  // them to pick one rather than guessing.
  const entityId =
    ctx.isConsolidated || !ctx.activeEntity ? null : ctx.activeEntity.id;
  const entitySlug =
    ctx.isConsolidated || !ctx.activeEntity ? null : ctx.activeEntity.slug;

  return (
    <AppShell>
      <PageShell>
        <SectionHeader
          title="Form W-4P — Pension Withholding"
          subtitle="Review, self-certify, and download your filled W-4P"
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

        {entityId ? (
          <W4pClient
            workspaceId={workspace.id}
            workspaceSlug={workspace.slug}
            entityId={entityId}
            entitySlug={entitySlug}
          />
        ) : (
          <Card>
            <p className="text-sm text-ink-secondary">
              A W-4P is prepared for a single person or entity. Pick a specific
              entity (use the selector above) to review its Form W-4P — the
              consolidated view can&apos;t hold a personal withholding form.
            </p>
          </Card>
        )}
      </PageShell>
    </AppShell>
  );
}
