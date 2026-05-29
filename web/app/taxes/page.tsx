// /taxes — Multi-year / multi-entity tax status dashboard.
//
// Server component: fetches tax-status aggregate from FastAPI, renders the
// TaxDashboard client component. Falls back to the per-year card list if the
// new endpoint is unavailable (e.g. during a rolling deploy).

import Link from "next/link";
import { AppShell } from "@/components/app-shell";
import { EntitySelector } from "@/components/entity-selector";
import { Card, PageShell, SectionHeader } from "@/components/primitives";
import { getActiveContext } from "@/lib/active-context";
import { fetchTaxStatus } from "@/lib/tax-status";
import { TaxDashboard } from "./dashboard";

export const dynamic = "force-dynamic";

export default async function TaxesPage() {
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
            title="Taxes"
            subtitle="Multi-year filing status by entity"
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

  const taxStatus = await fetchTaxStatus(workspace.id);

  return (
    <AppShell>
      <PageShell>
        <SectionHeader
          title="Taxes"
          subtitle="Multi-year filing status — for CPA review only, not filed returns."
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

        {/* DRAFT disclaimer */}
        <div className="mb-6 rounded-lg border border-pending/40 bg-pending/[0.06] px-4 py-3 text-sm text-pending">
          <strong>DRAFT — For CPA review only.</strong> This section aggregates
          Form 1120 data from your journal entries. The CPA reviews, adjusts,
          and files. No submission to the IRS or SCDOR happens from this
          platform.
        </div>

        {taxStatus ? (
          <TaxDashboard taxStatus={taxStatus} />
        ) : (
          /* Graceful degradation: link to per-year pages directly */
          <Card>
            <p className="text-sm text-ink-secondary">
              Could not load tax status dashboard. View individual years:{" "}
              {[2025, 2024, 2023, 2022].map((year) => (
                <Link
                  key={year}
                  href={`/taxes/${year}`}
                  className="mr-2 text-brand-400 underline-offset-2 hover:underline"
                >
                  {year}
                </Link>
              ))}
            </p>
          </Card>
        )}
      </PageShell>
    </AppShell>
  );
}
