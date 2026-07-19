import Link from "next/link";
import { AppShell } from "@/components/app-shell";
import { EntitySelector } from "@/components/entity-selector";
import { Card, PageShell, SectionHeader } from "@/components/primitives";
import { getActiveContext } from "@/lib/active-context";
import { PrepareClient } from "./prepare-client";

export const dynamic = "force-dynamic";

export default async function PrepareFormPage({
  params,
}: {
  // In Next.js 15, dynamic route params are a Promise and must be awaited.
  params: Promise<{ kind: string }>;
}) {
  const { kind } = await params;

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
            title="Prepare a form"
            subtitle="Fill out a tax form on behalf of a client"
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

  // A form is prepared for a SINGLE entity, so this screen needs ONE concrete
  // entity. When the user is in the consolidated view (or has no entity), ask
  // them to pick one rather than guessing — same rule the W-4P page uses.
  const entityId =
    ctx.isConsolidated || !ctx.activeEntity ? null : ctx.activeEntity.id;

  const subtitle = entityId
    ? `Filling on behalf of ${ctx.activeEntity?.display_name} — creates a draft to review, sign, and file`
    : "Pick a specific entity to prepare a form on its behalf";

  return (
    <AppShell>
      <PageShell>
        <SectionHeader
          title="Prepare a form"
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

        {entityId ? (
          <PrepareClient
            workspaceId={workspace.id}
            entityId={entityId}
            kind={kind}
          />
        ) : (
          <Card>
            <p className="text-sm text-ink-secondary">
              A tax form is prepared for a single person or entity. Pick a
              specific entity (use the selector above) to fill out this form on
              its behalf — the consolidated view can&apos;t hold a single
              client&apos;s form.
            </p>
          </Card>
        )}
      </PageShell>
    </AppShell>
  );
}
