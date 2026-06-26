import Link from "next/link";
import { ArrowLeft } from "lucide-react";
import { AppShell } from "@/components/app-shell";
import { EntitySelector } from "@/components/entity-selector";
import { Card, PageShell, SectionHeader } from "@/components/primitives";
import { getActiveContext } from "@/lib/active-context";
import { WpDetailClient } from "./wp-detail-client";

export const dynamic = "force-dynamic";

export default async function WorkProductDetailPage({
  params,
}: {
  // In Next.js 15, dynamic route params are a Promise and must be awaited.
  params: Promise<{ wpId: string }>;
}) {
  const { wpId } = await params;

  const ctx = await getActiveContext();
  const workspace = ctx.activeWorkspace;
  const canConsolidate = workspace
    ? ["owner", "admin"].includes(workspace.role)
    : false;

  const backLink = (
    <Link
      href="/taxes/work-products"
      className="inline-flex items-center gap-1.5 rounded-md border border-default bg-surface px-3 py-1.5 text-xs font-medium text-ink-secondary transition hover:bg-elevated hover:text-ink-primary"
    >
      <ArrowLeft size={13} />
      All work products
    </Link>
  );

  if (!workspace) {
    return (
      <AppShell>
        <PageShell>
          <SectionHeader title="Work product" subtitle="Review, sign, and file" />
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

  // A work product belongs to a single entity, so this screen needs ONE
  // concrete entity — same rule as the list / W-4P / prepare screens.
  const entityId =
    ctx.isConsolidated || !ctx.activeEntity ? null : ctx.activeEntity.id;
  const entitySlug =
    ctx.isConsolidated || !ctx.activeEntity ? null : ctx.activeEntity.slug;

  return (
    <AppShell>
      <PageShell>
        <SectionHeader
          title="Work product"
          subtitle="Review, sign, and file"
          action={
            <>
              {backLink}
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
          <WpDetailClient
            workspaceId={workspace.id}
            entityId={entityId}
            entitySlug={entitySlug}
            wpId={wpId}
          />
        ) : (
          <Card>
            <p className="text-sm text-ink-secondary">
              A work product belongs to a single person or entity. Pick a
              specific entity (use the selector above) to open this form — the
              consolidated view can&apos;t hold a single client&apos;s work
              product.
            </p>
          </Card>
        )}
      </PageShell>
    </AppShell>
  );
}
