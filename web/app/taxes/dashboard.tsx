"use client";

// TaxDashboard — multi-year / multi-entity status dashboard.
//
// Rendered by /taxes/page.tsx (server component) which fetches TaxStatusResponse
// and passes it down. Client component so it can manage the specialist modal state.

import { useState } from "react";
import Link from "next/link";
import {
  AlertTriangle,
  Building2,
  CalendarClock,
  CheckCircle2,
  Clock4,
  FilePen,
  X
} from "lucide-react";
import { cn } from "@/lib/cn";
import { Card, KpiCard, StatusChip } from "@/components/primitives";
import {
  type EntityTaxStatus,
  type TaxYearStatus,
  type FilingStatus,
  type TaxStatusResponse,
  countByFilingStatus,
  getOverdueItems
} from "@/lib/tax-status";
import { getAnnotation, getEntityAnnotations } from "@/lib/tax-status-annotations";

// ---------------------------------------------------------------------------
// Year tile colors (matches spec: red / amber / violet / green / grey)
// ---------------------------------------------------------------------------

type YearTileColor = {
  border: string;
  bg: string;
  text: string;
  label: string;
};

const STATUS_COLORS: Record<FilingStatus, YearTileColor> = {
  overdue: {
    border: "border-debit",
    bg: "bg-debit/10",
    text: "text-debit",
    label: "Overdue"
  },
  draft: {
    border: "border-pending",
    bg: "bg-pending/10",
    text: "text-pending",
    label: "Draft"
  },
  cpa_review: {
    border: "border-[#7c3aed]",
    bg: "bg-[#7c3aed]/10",
    text: "text-[#7c3aed]",
    label: "CPA Review"
  },
  filed: {
    border: "border-credit",
    bg: "bg-credit/10",
    text: "text-credit",
    label: "Filed"
  },
  no_draft: {
    border: "border-subtle",
    bg: "bg-white/[0.02]",
    text: "text-ink-tertiary",
    label: "No draft"
  }
};

// ---------------------------------------------------------------------------
// Specialist modal
// ---------------------------------------------------------------------------

type SpecialistModalProps = {
  entity: EntityTaxStatus;
  targetYear: number;
  onClose: () => void;
};

function SpecialistModal({ entity, targetYear, onClose }: SpecialistModalProps) {
  // Build a representative docker exec command for the 1120 preparer specialist.
  // Brigade integration is future work; for now show the manual CLI invocation.
  const cmd = [
    `docker exec -it accounting-ops-workers-1 \\`,
    `  python -m agents.tax_specialists.form_1120_preparer.run_specialist \\`,
    `  --workspace-id <WORKSPACE_ID> \\`,
    `  --entity-id ${entity.entity_id} \\`,
    `  --tax-year ${targetYear}`
  ].join("\n");

  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-black/60 p-4"
      onClick={onClose}
    >
      <div
        className="relative w-full max-w-xl rounded-lg border border-default bg-surface p-6 shadow-2xl"
        onClick={(e) => e.stopPropagation()}
      >
        <button
          onClick={onClose}
          className="absolute right-4 top-4 text-ink-tertiary hover:text-ink-primary"
          aria-label="Close modal"
        >
          <X size={18} />
        </button>

        <h2 className="text-base font-semibold">
          Run specialists — {entity.display_name} {targetYear}
        </h2>
        <p className="mt-1 text-sm text-ink-secondary">
          Brigade agent dispatch is future work. For now, run the 1120 Preparer
          specialist manually via the CLI below (requires SSH to your-host or a
          local docker stack).
        </p>

        <div className="mt-4 rounded-lg border border-subtle bg-black/30 p-3">
          <pre className="overflow-x-auto whitespace-pre text-[11px] text-ink-primary font-mono leading-relaxed">
            {cmd}
          </pre>
        </div>

        <p className="mt-3 text-[11px] text-ink-tertiary">
          Replace <code>&lt;WORKSPACE_ID&gt;</code> with your workspace UUID.
          The specialist will insert rows into{" "}
          <code>tax_review_queue</code> for CPA review on return.
        </p>

        <div className="mt-5 flex justify-end">
          <button
            onClick={onClose}
            className="rounded-lg bg-brand-500/15 px-4 py-2 text-sm font-medium text-brand-400 hover:bg-brand-500/25 transition"
          >
            Close
          </button>
        </div>
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Year tile
// ---------------------------------------------------------------------------

type YearTileProps = {
  year: TaxYearStatus;
  entityId: string;
};

function YearTile({ year, entityId }: YearTileProps) {
  const colors = STATUS_COLORS[year.filing_status];
  const queueTotal =
    (year.tax_review_queue_summary.pending ?? 0) +
    (year.tax_review_queue_summary.reviewed ?? 0) +
    (year.tax_review_queue_summary.resolved ?? 0);

  return (
    <Link
      href={`/taxes/${year.tax_year}?entity_id=${entityId}`}
      className={cn(
        "group flex flex-col gap-1 rounded-lg border px-3 py-2 min-w-[88px] transition hover:opacity-80",
        colors.border,
        colors.bg
      )}
      title={`${year.tax_year} — ${colors.label}`}
    >
      <div className="flex items-center justify-between gap-1">
        <span className="font-mono text-xs font-semibold">{year.tax_year}</span>
        {year.is_short_year && (
          <span className="rounded bg-white/10 px-1 text-[9px] uppercase tracking-wide text-ink-tertiary">
            SY
          </span>
        )}
      </div>
      <span className={cn("text-[10px] font-medium uppercase tracking-wide", colors.text)}>
        {colors.label}
      </span>
      {queueTotal > 0 && (
        <span className="text-[10px] text-ink-tertiary">
          {year.tax_review_queue_summary.pending}p / {queueTotal} items
        </span>
      )}
      {year.last_specialist_run && (
        <span className="text-[9px] text-ink-tertiary truncate">
          Last run: {new Date(year.last_specialist_run.started_at).toLocaleDateString()}
        </span>
      )}
    </Link>
  );
}

// ---------------------------------------------------------------------------
// Entity card
// ---------------------------------------------------------------------------

type EntityCardProps = {
  entity: EntityTaxStatus;
  onRunSpecialists: (entity: EntityTaxStatus, year: number) => void;
};

function EntityCard({ entity, onRunSpecialists }: EntityCardProps) {
  const entityAnnotations = getEntityAnnotations(entity.entity_slug ?? "");
  const hasOverdue = entity.years.some((y) => y.filing_status === "overdue");

  // Find the next year that has no specialist run and isn't filed
  const nextUncoveredYear = entity.years.find(
    (y) =>
      y.filing_status !== "filed" &&
      y.last_specialist_run === null
  );

  return (
    <Card padded={false} className={cn(hasOverdue ? "border-debit/60" : undefined)}>
      {/* Card header */}
      <div className="flex flex-wrap items-start justify-between gap-3 px-5 py-4 border-b border-subtle">
        <div className="min-w-0">
          <div className="flex flex-wrap items-center gap-2">
            <Building2 size={16} className="shrink-0 text-ink-tertiary" />
            <span className="font-semibold text-sm">{entity.display_name}</span>
            <span className="inline-flex items-center rounded-full bg-white/5 px-2 py-0.5 text-[10px] font-medium uppercase tracking-wider text-ink-secondary">
              {entity.entity_type}
            </span>
            {entity.jurisdictions.map((j) => (
              <span
                key={j}
                className="inline-flex items-center rounded-full bg-agent-500/10 px-2 py-0.5 text-[10px] font-medium uppercase tracking-wider text-agent-500"
              >
                {j}
              </span>
            ))}
          </div>
          {entity.incorporated_at && (
            <p className="mt-0.5 text-[11px] text-ink-tertiary">
              Incorporated {entity.incorporated_at}
            </p>
          )}
        </div>

        {/* Action: run specialists */}
        {nextUncoveredYear && (
          <button
            onClick={() =>
              onRunSpecialists(entity, nextUncoveredYear.tax_year)
            }
            className="shrink-0 rounded-lg border border-brand-500/30 bg-brand-500/10 px-3 py-1.5 text-xs font-medium text-brand-400 hover:bg-brand-500/20 transition"
          >
            Run specialists for {nextUncoveredYear.tax_year}
          </button>
        )}
      </div>

      {/* Year strip */}
      <div className="flex flex-wrap gap-2 px-5 py-4">
        {entity.years.map((year) => (
          <YearTile key={year.tax_year} year={year} entityId={entity.entity_id} />
        ))}
        {entity.years.length === 0 && (
          <p className="text-sm text-ink-tertiary">No active tax years.</p>
        )}
      </div>

      {/* CPA notes */}
      {entityAnnotations.length > 0 && (
        <div className="border-t border-subtle px-5 py-4 space-y-3">
          <p className="text-[10px] font-medium uppercase tracking-wider text-ink-tertiary">
            CPA Notes
          </p>
          {entityAnnotations.map((ann) => (
            <div
              key={ann.tax_year}
              className={cn(
                "rounded-lg border px-3 py-2.5 text-sm",
                ann.tone === "critical"
                  ? "border-debit/40 bg-debit/[0.06] text-debit"
                  : ann.tone === "warning"
                    ? "border-pending/40 bg-pending/[0.06] text-pending"
                    : "border-subtle bg-white/[0.02] text-ink-secondary"
              )}
            >
              <p className="font-medium text-[11px] uppercase tracking-wide mb-1">
                {ann.tax_year}
              </p>
              <p className="font-semibold text-[12px] mb-0.5">{ann.headline}</p>
              <p className="text-[12px] leading-relaxed">{ann.detail}</p>
            </div>
          ))}
        </div>
      )}
    </Card>
  );
}

// ---------------------------------------------------------------------------
// Overdue banner
// ---------------------------------------------------------------------------

function OverdueBanner({
  items
}: {
  items: Array<{ entity: EntityTaxStatus; year: TaxYearStatus }>;
}) {
  const [dismissed, setDismissed] = useState(false);
  if (dismissed || items.length === 0) return null;

  return (
    <div className="relative mb-6 rounded-lg border border-debit/50 bg-debit/[0.08] px-4 py-4">
      <button
        onClick={() => setDismissed(true)}
        className="absolute right-3 top-3 text-debit/60 hover:text-debit transition"
        aria-label="Dismiss banner"
      >
        <X size={14} />
      </button>
      <div className="flex items-start gap-3">
        <AlertTriangle size={18} className="mt-0.5 shrink-0 text-debit" />
        <div>
          <p className="text-sm font-semibold text-debit">
            {items.length === 1
              ? "1 overdue filing requires immediate attention"
              : `${items.length} overdue filings require immediate attention`}
          </p>
          <ul className="mt-2 space-y-1.5">
            {items.map(({ entity, year }) => {
              const ann = getAnnotation(entity.entity_slug ?? "", year.tax_year);
              return (
                <li key={`${entity.entity_id}:${year.tax_year}`} className="text-sm text-debit/90">
                  <Link
                    href={`/taxes/${year.tax_year}?entity_id=${entity.entity_id}`}
                    className="font-medium underline-offset-2 hover:underline"
                  >
                    {entity.display_name} — {year.tax_year}
                  </Link>
                  {ann && (
                    <span className="ml-2 text-[11px] text-debit/70">
                      {ann.headline}
                    </span>
                  )}
                </li>
              );
            })}
          </ul>
        </div>
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Stat bar
// ---------------------------------------------------------------------------

function StatBar({
  entityCount,
  overdue,
  inDraft,
  filed
}: {
  entityCount: number;
  overdue: number;
  inDraft: number;
  filed: number;
}) {
  return (
    <div className="mb-6 grid grid-cols-2 gap-3 sm:grid-cols-4">
      <KpiCard
        label="Entities"
        value={String(entityCount)}
      />
      <KpiCard
        label="Overdue returns"
        value={String(overdue)}
        delta={overdue > 0 ? "Action required" : "None"}
        deltaTone={overdue > 0 ? "debit" : "credit"}
      />
      <KpiCard
        label="In draft / CPA review"
        value={String(inDraft)}
        deltaTone="pending"
      />
      <KpiCard
        label="Filed"
        value={String(filed)}
        deltaTone="credit"
      />
    </div>
  );
}

// ---------------------------------------------------------------------------
// Main dashboard export
// ---------------------------------------------------------------------------

export function TaxDashboard({
  taxStatus
}: {
  taxStatus: TaxStatusResponse;
}) {
  const [modalState, setModalState] = useState<{
    entity: EntityTaxStatus;
    year: number;
  } | null>(null);

  const entities = taxStatus.entities;
  const overdueItems = getOverdueItems(entities);

  const entityCount = entities.length;
  const overdue = countByFilingStatus(entities, "overdue");
  const inDraft =
    countByFilingStatus(entities, "draft") +
    countByFilingStatus(entities, "cpa_review");
  const filed = countByFilingStatus(entities, "filed");

  return (
    <>
      {/* Overdue banner */}
      <OverdueBanner items={overdueItems} />

      {/* Stat bar */}
      <StatBar
        entityCount={entityCount}
        overdue={overdue}
        inDraft={inDraft}
        filed={filed}
      />

      {/* Per-entity cards */}
      <div className="space-y-4">
        {entities.length === 0 && (
          <Card>
            <p className="text-sm text-ink-secondary">
              No entities to track yet. New here?{" "}
              <Link
                href="/connectors"
                className="text-brand-400 underline-offset-2 hover:underline"
              >
                Connect your bank
              </Link>{" "}
              or{" "}
              <Link
                href="/documents"
                className="text-brand-400 underline-offset-2 hover:underline"
              >
                drop in a few documents
              </Link>{" "}
              to get started — your transactions, reports, and taxes fill in from
              there.
            </p>
          </Card>
        )}
        {entities.map((entity) => (
          <EntityCard
            key={entity.entity_id}
            entity={entity}
            onRunSpecialists={(ent, year) =>
              setModalState({ entity: ent, year })
            }
          />
        ))}
      </div>

      {/* Legend */}
      <div className="mt-6 flex flex-wrap gap-3 text-[11px] text-ink-tertiary">
        {(Object.entries(STATUS_COLORS) as [FilingStatus, YearTileColor][]).map(
          ([status, colors]) => (
            <span key={status} className="flex items-center gap-1.5">
              <span
                className={cn(
                  "inline-block h-2.5 w-2.5 rounded-sm border",
                  colors.border,
                  colors.bg
                )}
              />
              {colors.label}
            </span>
          )
        )}
        <span className="flex items-center gap-1.5">
          <span className="rounded bg-white/10 px-1 text-[9px] uppercase tracking-wide text-ink-tertiary">
            SY
          </span>
          Short year
        </span>
      </div>

      <p className="mt-3 text-[11px] text-ink-tertiary">
        as of {taxStatus.as_of} — data from posted journal entries only. CPA
        must review all lines before filing.
      </p>

      {/* Specialist modal */}
      {modalState && (
        <SpecialistModal
          entity={modalState.entity}
          targetYear={modalState.year}
          onClose={() => setModalState(null)}
        />
      )}
    </>
  );
}
