import Link from "next/link";
import { ArrowRight } from "lucide-react";
import { AppShell } from "@/components/app-shell";
import { EntitySelector } from "@/components/entity-selector";
import { Card, PageShell, SectionHeader } from "@/components/primitives";
import { getActiveContext } from "@/lib/active-context";
import { WorkProductsClient } from "./work-products-client";

export const dynamic = "force-dynamic";

export default async function WorkProductsPage() {
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
            title="Work products"
            subtitle="Every form you've prepared — review, sign, and file"
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

  // A work product belongs to a single person/entity, so this screen needs ONE
  // concrete entity. When the user is in the consolidated view (or has no
  // entity), ask them to pick one rather than guessing — same rule the W-4P and
  // prepare screens use.
  const entityId =
    ctx.isConsolidated || !ctx.activeEntity ? null : ctx.activeEntity.id;
  const entitySlug =
    ctx.isConsolidated || !ctx.activeEntity ? null : ctx.activeEntity.slug;

  return (
    <AppShell>
      <PageShell>
        <SectionHeader
          title="Work products"
          subtitle="Every form you've prepared — review, sign, and file"
          action={
            <>
              <Link
                href="/taxes/forms"
                className="inline-flex items-center gap-1.5 rounded-md bg-brand-500 px-3 py-1.5 text-sm font-medium text-white transition hover:bg-brand-600"
              >
                Prepare a form
                <ArrowRight size={14} />
              </Link>
              <EntitySelector
                entities={ctx.entities}
                activeEntityId={
                  ctx.isConsolidated ? "consolidated" : ctx.activeEntity?.id
                }
                canConsolidate={canConsolidate}
              />
            </>
          }
        />

        {entityId ? (
          <WorkProductsClient
            workspaceId={workspace.id}
            entityId={entityId}
            entitySlug={entitySlug}
          />
        ) : (
          <Card>
            <p className="text-sm text-ink-secondary">
              Work products are prepared for a single person or entity. Pick a
              specific entity (use the selector above) to see its prepared forms
              — the consolidated view can&apos;t hold a single client&apos;s
              work products.
            </p>
          </Card>
        )}
      </PageShell>
    </AppShell>
  );
}
