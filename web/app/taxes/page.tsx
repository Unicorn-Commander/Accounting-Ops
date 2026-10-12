// /taxes — Multi-year / multi-entity tax status dashboard.
//
// Server component: fetches tax-status aggregate from FastAPI, renders the
// TaxDashboard client component. Falls back to the per-year card list if the
// new endpoint is unavailable (e.g. during a rolling deploy).

import Link from "next/link";
import { ArrowRight, FileText, Landmark } from "lucide-react";
import { AppShell } from "@/components/app-shell";
import { EntitySelector } from "@/components/entity-selector";
import { Card, PageShell, SectionHeader } from "@/components/primitives";
import { getActiveContext } from "@/lib/active-context";
import { fetchTaxStatus, filterTaxStatusByEntity } from "@/lib/tax-status";
import { TaxDashboard } from "./dashboard";

export const dynamic = "force-dynamic";

export default async function TaxesPage(props: {
  searchParams: Promise<{ entity_id?: string | string[] }>;
}) {
  const searchParams = await props.searchParams;
  const rawEntityId = searchParams.entity_id;
  const requestedEntityId = Array.isArray(rawEntityId)
    ? rawEntityId[0]
    : rawEntityId;
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
  const filteredEntity = requestedEntityId
    ? ctx.entities.find((entity) => entity.id === requestedEntityId) ?? null
    : null;
  const visibleTaxStatus = taxStatus && filteredEntity
    ? filterTaxStatusByEntity(taxStatus, filteredEntity.id)
    : taxStatus;

  // First-run: no tax data to show yet means the books are still empty (no
  // posted entries, so nothing to draft a return or estimate from). Show a warm
  // starting-point hint instead of a bare, seemingly-broken dashboard.
  const hasTaxData = !!visibleTaxStatus?.entities.some((e) => e.years.length > 0);

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

        {filteredEntity && (
          <div className="mb-6 flex flex-wrap items-center justify-between gap-2 rounded-lg border border-brand-500/30 bg-brand-500/[0.05] px-4 py-3 text-sm text-ink-secondary">
            <span>
              Showing filing status for <strong className="text-ink-primary">{filteredEntity.display_name}</strong> only.
            </span>
            <Link
              href="/taxes"
              className="text-xs font-medium text-brand-400 underline-offset-2 hover:underline"
            >
              Show all entities
            </Link>
          </div>
        )}

        {/* DRAFT disclaimer */}
        <div className="mb-6 rounded-lg border border-pending/40 bg-pending/[0.06] px-4 py-3 text-sm text-pending">
          <strong>DRAFT — For CPA review only.</strong> This section aggregates
          Form 1120 data from your journal entries. The CPA reviews, adjusts,
          and files. No submission to the IRS or SCDOR happens from this
          platform.
        </div>

        {/* First-run hint — filing deadlines + estimates need books first. */}
        {!hasTaxData && (
          <div className="mb-6 rounded-lg border border-default bg-surface px-5 py-4">
            <p className="text-sm font-medium text-ink-primary">
              Your filing deadlines and tax estimates will appear here once your
              books have entries.
            </p>
            <p className="mt-1 text-xs leading-relaxed text-ink-tertiary">
              New here? Connect your bank or drop in a few documents to get
              started — your transactions, reports, and taxes fill in from there.
            </p>
            <div className="mt-3 flex flex-wrap gap-2">
              <Link
                href="/connectors"
                className="inline-flex items-center gap-1 rounded-lg bg-brand-500/15 px-3 py-1.5 text-xs font-medium text-brand-400 transition hover:bg-brand-500/25"
              >
                <Landmark size={12} /> Connect your bank <ArrowRight size={12} />
              </Link>
              <Link
                href="/documents"
                className="inline-flex items-center gap-1 rounded-lg border border-default px-3 py-1.5 text-xs font-medium text-ink-secondary transition hover:bg-white/[0.03] hover:text-ink-primary"
              >
                <FileText size={12} /> Upload documents
              </Link>
            </div>
          </div>
        )}

        {visibleTaxStatus ? (
          <TaxDashboard taxStatus={visibleTaxStatus} />
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
