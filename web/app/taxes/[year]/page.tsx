// /taxes/[year] — Per-year Form 1120 view.
// Server component that loads the package and passes it to the client UI.

import Link from "next/link";
import { notFound } from "next/navigation";
import { ArrowLeft } from "lucide-react";
import { AppShell } from "@/components/app-shell";
import { EntitySelector } from "@/components/entity-selector";
import { Card, PageShell, SectionHeader } from "@/components/primitives";
import { getActiveContext } from "@/lib/active-context";
import { fetch1120Package } from "@/lib/taxes-1120";
import { Form1120View } from "./form-1120-view";

export const dynamic = "force-dynamic";

const VALID_YEARS = new Set(["2022", "2023", "2024", "2025"]);

export default async function TaxYearPage(props: {
  params: Promise<{ year: string }>;
}) {
  const { year } = await props.params;

  if (!VALID_YEARS.has(year) || !/^\d{4}$/.test(year)) {
    notFound();
  }

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
            title={`Form 1120 — ${year}`}
            subtitle="No workspace selected"
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
    ctx.isConsolidated || !ctx.activeEntity ? "all" : ctx.activeEntity.id;
  const entityDisplayName = ctx.isConsolidated
    ? "All entities (consolidated)"
    : ctx.activeEntity?.display_name ?? "—";

  let pkg = null;
  let fetchError: string | null = null;
  try {
    pkg = await fetch1120Package(workspace.id, entityId, parseInt(year, 10));
  } catch (e) {
    fetchError =
      e instanceof Error ? e.message : "Failed to load Form 1120 package.";
  }

  return (
    <AppShell>
      <PageShell>
        <div className="mb-4">
          <Link
            href="/taxes"
            className="inline-flex items-center gap-1 text-xs text-ink-tertiary hover:text-ink-primary"
          >
            <ArrowLeft size={13} /> Back to Taxes
          </Link>
        </div>

        <SectionHeader
          title={`Form 1120 — ${year}`}
          subtitle={`${entityDisplayName} · U.S. Corporation Income Tax Return (CPA review package)`}
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

        {fetchError ? (
          <Card>
            <p className="text-sm text-debit">{fetchError}</p>
            <p className="mt-1 text-xs text-ink-tertiary">
              Make sure the entity has posted journal entries for {year} and the
              cpa-export service is running.
            </p>
          </Card>
        ) : (
          <Form1120View
            pkg={pkg}
            workspaceId={workspace.id}
            workspaceSlug={workspace.slug}
            entityId={entityId}
            entityDisplayName={entityDisplayName}
            year={parseInt(year, 10)}
          />
        )}
      </PageShell>
    </AppShell>
  );
}
