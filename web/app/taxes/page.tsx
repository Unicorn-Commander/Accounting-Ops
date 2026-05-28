// /taxes — Taxes landing: lists fiscal years with 1120 summary data for CPA review.
// This is a CPA-aid page: data is for review only, not a filed return.

import Link from "next/link";
import { FileText } from "lucide-react";
import { AppShell } from "@/components/app-shell";
import { EntitySelector } from "@/components/entity-selector";
import { Card, PageShell, SectionHeader } from "@/components/primitives";
import { getActiveContext } from "@/lib/active-context";
import { cn } from "@/lib/cn";
import { fetch1120Package, type Form1120Package } from "@/lib/taxes-1120";

export const dynamic = "force-dynamic";

const YEARS = [2025, 2024, 2023, 2022] as const;

function formatDollars(cents: number): string {
  const dollars = cents / 100;
  const abs = Math.abs(dollars);
  const formatted = new Intl.NumberFormat("en-US", {
    style: "currency",
    currency: "USD",
    minimumFractionDigits: 0,
    maximumFractionDigits: 0
  }).format(abs);
  return dollars < 0 ? `(${formatted})` : formatted;
}

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
            subtitle="Pre-filled Form 1120 packages for CPA review"
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
  const entityDisplayName = ctx.isConsolidated
    ? "All entities (consolidated)"
    : ctx.activeEntity?.display_name ?? "—";

  // Attempt to load the most recent year's package for a summary.
  // Years without data return null — we still show the row so the CPA
  // can see the entity exists and navigate to the blank form.
  const summaries: Array<{
    year: number;
    pkg: Form1120Package | null;
  }> = await Promise.all(
    YEARS.map(async (year) => {
      try {
        const pkg = await fetch1120Package(
          workspace.id,
          entityId ?? "all",
          year
        );
        return { year, pkg };
      } catch {
        return { year, pkg: null };
      }
    })
  );

  const hasAny = summaries.some((s) => s.pkg !== null);

  return (
    <AppShell>
      <PageShell>
        <SectionHeader
          title="Taxes"
          subtitle="Pre-filled Form 1120 packages for CPA review — not filed returns."
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

        {/* Disclaimer banner */}
        <div className="mb-6 rounded-lg border border-pending/40 bg-pending/[0.06] px-4 py-3 text-sm text-pending">
          <strong>DRAFT — For CPA review only.</strong> This section produces a
          Form 1120 data package from your journal entries. The CPA reviews,
          adjusts, and files. No submission to the IRS happens from this
          platform.
        </div>

        <Card padded={false}>
          <div className="border-b border-subtle px-5 py-3 text-[10px] font-medium uppercase tracking-wider text-ink-tertiary">
            {entityDisplayName} — Fiscal Years Available
          </div>

          {!hasAny && (
            <p className="px-5 py-4 text-sm text-ink-tertiary">
              No journal data found for the selected entity. Post entries and
              return here to review the Form 1120 package.
            </p>
          )}

          <div className="divide-y divide-subtle/60">
            {summaries.map(({ year, pkg }) => {
              const hasData = pkg !== null && (
                pkg.income.lines.length > 0 ||
                pkg.deductions.lines.length > 0
              );
              const taxableIncome = pkg?.taxable_income_before_nol_cents ?? null;
              const totalIncome = pkg?.income.total_income_cents ?? null;
              const totalDeductions = pkg?.deductions.total_deductions_cents ?? null;

              return (
                <Link
                  key={year}
                  href={`/taxes/${year}`}
                  className={cn(
                    "flex items-center gap-4 px-5 py-4 transition hover:bg-elevated",
                    !hasData && "opacity-60"
                  )}
                >
                  <div className="flex h-10 w-10 shrink-0 items-center justify-center rounded-md bg-elevated text-brand-400 ring-1 ring-inset ring-brand-500/30">
                    <FileText size={18} />
                  </div>

                  <div className="min-w-0 flex-1">
                    <div className="flex items-center gap-2">
                      <span className="font-semibold">Form 1120 — {year}</span>
                      {hasData ? (
                        <span className="inline-flex items-center rounded-full bg-credit/15 px-2 py-0.5 text-[10px] font-medium uppercase tracking-wider text-credit">
                          Data available
                        </span>
                      ) : (
                        <span className="inline-flex items-center rounded-full bg-white/5 px-2 py-0.5 text-[10px] font-medium uppercase tracking-wider text-ink-tertiary">
                          No data yet
                        </span>
                      )}
                    </div>
                    {hasData && (
                      <div className="mt-1 flex flex-wrap gap-4 text-xs text-ink-secondary">
                        {totalIncome !== null && (
                          <span>
                            Revenue:{" "}
                            <span className="font-mono text-credit">
                              {formatDollars(totalIncome)}
                            </span>
                          </span>
                        )}
                        {totalDeductions !== null && (
                          <span>
                            Deductions:{" "}
                            <span className="font-mono text-debit">
                              {formatDollars(totalDeductions)}
                            </span>
                          </span>
                        )}
                        {taxableIncome !== null && (
                          <span>
                            Taxable income (before NOL):{" "}
                            <span
                              className={cn(
                                "font-mono",
                                taxableIncome >= 0
                                  ? "text-credit"
                                  : "text-debit"
                              )}
                            >
                              {formatDollars(taxableIncome)}
                            </span>
                          </span>
                        )}
                      </div>
                    )}
                  </div>

                  <span className="shrink-0 text-xs text-ink-tertiary">
                    View &rarr;
                  </span>
                </Link>
              );
            })}
          </div>
        </Card>

        <p className="mt-4 text-[11px] text-ink-tertiary">
          Fiscal years 2022–2025 shown. Data derived from posted journal
          entries only. CPA must review all lines, apply depreciation
          schedules, NOL carryforwards, and any required elections before
          filing.
        </p>
      </PageShell>
    </AppShell>
  );
}
