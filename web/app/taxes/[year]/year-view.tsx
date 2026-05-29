"use client";

// YearView — four-tab wrapper for the /taxes/[year] page.
//
// Tab "Manual"          (default) — the existing Form1120View review surface.
// Tab "Specialist draft"          — the SpecialistDraftView for the AI queue.
// Tab "Run history"               — the RunHistoryView MLOps observability table.
// Tab "IRS Forms"                 — IrsFormsSection: download pre-filled PDFs.
//
// Tabs use local state so navigating between them is instant (no server round-trip).
// The active tab is NOT reflected in the URL — purely a UI concern within /taxes/[year].

import { useState } from "react";
import { cn } from "@/lib/cn";
import type { Form1120Package } from "@/lib/taxes-1120";
import type { TaxReviewItem } from "@/lib/tax-review-queue";
import type { SpecialistRun } from "@/lib/specialist-runs";
import { Form1120View } from "./form-1120-view";
import { IrsFormsSection } from "./irs-forms-section";
import { SpecialistDraftView } from "./specialist-draft-view";
import { RunHistoryView } from "./run-history-view";
import { Card } from "@/components/primitives";

type Tab = "manual" | "specialist" | "runs" | "irs-forms";

export type YearViewProps = {
  pkg: Form1120Package | null;
  workspaceId: string;
  workspaceSlug: string;
  entityId: string;
  entityDisplayName: string;
  year: number;
  taxReviewItems: TaxReviewItem[];
  specialistRuns: SpecialistRun[];
};

export function YearView({
  pkg,
  workspaceId,
  workspaceSlug,
  entityId,
  entityDisplayName,
  year,
  taxReviewItems,
  specialistRuns
}: YearViewProps) {
  const [activeTab, setActiveTab] = useState<Tab>("manual");

  const pendingCount = taxReviewItems.filter((it) => it.status === "pending").length;
  const runningCount = specialistRuns.filter((r) => r.status === "running").length;

  const tabs: { id: Tab; label: string; badge?: number }[] = [
    { id: "manual", label: "Manual" },
    {
      id: "specialist",
      label: "Specialist draft",
      badge: pendingCount > 0 ? pendingCount : undefined
    },
    {
      id: "runs",
      label: "Run history",
      badge: runningCount > 0 ? runningCount : undefined
    },
    { id: "irs-forms", label: "IRS Forms" }
  ];

  return (
    <div>
      {/* Tab bar — pill-button style matching reports/transactions pages */}
      <div className="mb-6 flex items-center gap-1.5">
        {tabs.map((t) => (
          <button
            key={t.id}
            type="button"
            onClick={() => setActiveTab(t.id)}
            className={cn(
              "relative inline-flex items-center gap-1.5 rounded-md border px-3 py-1.5 text-xs font-medium",
              activeTab === t.id
                ? "border-brand-500 bg-brand-500/10 text-brand-300"
                : "border-default bg-surface text-ink-secondary hover:bg-elevated"
            )}
          >
            {t.label}
            {t.badge != null && (
              <span
                className={cn(
                  "inline-flex h-4 min-w-[1rem] items-center justify-center rounded-full px-1 text-[10px] font-semibold",
                  activeTab === t.id
                    ? "bg-brand-500/30 text-brand-200"
                    : "bg-pending/20 text-pending"
                )}
              >
                {t.badge}
              </span>
            )}
          </button>
        ))}
      </div>

      {/* Tab panels */}
      {activeTab === "manual" && (
        <Form1120View
          pkg={pkg}
          workspaceId={workspaceId}
          workspaceSlug={workspaceSlug}
          entityId={entityId}
          entityDisplayName={entityDisplayName}
          year={year}
        />
      )}

      {activeTab === "specialist" && (
        <SpecialistDraftView
          items={taxReviewItems}
          workspaceId={workspaceId}
          year={year}
        />
      )}

      {activeTab === "runs" && (
        <RunHistoryView
          initialRuns={specialistRuns}
          workspaceId={workspaceId}
          entityId={entityId}
          year={year}
        />
      )}

      {activeTab === "irs-forms" && (
        <Card>
          <h3 className="mb-4 text-xs font-semibold uppercase tracking-wider text-ink-secondary">
            IRS Forms — Pre-filled DRAFT PDFs
          </h3>
          <IrsFormsSection
            workspaceId={workspaceId}
            entityId={entityId}
            year={year}
          />
        </Card>
      )}
    </div>
  );
}
