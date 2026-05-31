// /taxes/[year] — Per-year tax review surface.
//
// Routes by entity.kind to the appropriate form variant:
//   c_corp        -> Form 1120 (full year view: Narrative/Manual/Specialist/Runs/IRS Forms)
//   s_corp        -> Form 1120-S placeholder + chat handoff
//   llc           -> Form 1065 placeholder + chat handoff
//   partnership   -> Form 1065 placeholder + chat handoff
//   sole_prop     -> Schedule C placeholder + chat handoff
//   personal      -> Form 1040 placeholder + chat handoff
//   nonprofit     -> Form 990 placeholder + chat handoff
//   trust/estate  -> Form 1041 placeholder + chat handoff
//   unknown       -> Form 1120 fallback with banner
//
// Consolidated views always render the Form 1120 surface (it aggregates
// all entities; the C-Corp narrative is the most informative shape).

import Link from "next/link";
import { notFound } from "next/navigation";
import { ArrowLeft } from "lucide-react";
import { AppShell } from "@/components/app-shell";
import { EntitySelector } from "@/components/entity-selector";
import { Card, PageShell, SectionHeader } from "@/components/primitives";
import { getActiveContext } from "@/lib/active-context";
import { EntityKindRouter, titleFor } from "./entity-kind-router";

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
            title={`Taxes — ${year}`}
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

  const taxYear = parseInt(year, 10);

  // Title + subtitle reflect the form variant routed to (not always 1120).
  const { title, subtitle } = titleFor(
    ctx.isConsolidated,
    ctx.activeEntity,
    taxYear
  );

  return (
    <AppShell taxYear={taxYear}>
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
          title={title}
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

        {/* The router itself is a server component — it picks the form
            variant per entity.kind and either renders the full YearView
            (C-Corp) or a chat-handoff placeholder (everything else). */}
        <EntityKindRouter
          workspace={workspace}
          entity={ctx.activeEntity}
          entities={ctx.entities}
          isConsolidated={ctx.isConsolidated}
          canConsolidate={canConsolidate}
          year={taxYear}
        />
      </PageShell>
    </AppShell>
  );
}
