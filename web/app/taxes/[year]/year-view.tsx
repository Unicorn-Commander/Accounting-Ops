"use client";

// YearView — two-tab wrapper for the /taxes/[year] page.
//
// Tab "Manual"  (default) — the existing Form1120View review surface.
// Tab "Specialist draft"  — the new SpecialistDraftView for the AI queue.
//
// Tabs use a URL-searchParam approach would require a server round-trip;
// instead we use local state so navigating between tabs is instant.
// The active tab is NOT reflected in the URL — the tab is purely a UI
// concern within /taxes/[year].

import { useState } from "react";
import { cn } from "@/lib/cn";
import type { Form1120Package } from "@/lib/taxes-1120";
import type { TaxReviewItem } from "@/lib/tax-review-queue";
import { Form1120View } from "./form-1120-view";
import { SpecialistDraftView } from "./specialist-draft-view";

type Tab = "manual" | "specialist";

export type YearViewProps = {
  pkg: Form1120Package | null;
  workspaceId: string;
  workspaceSlug: string;
  entityId: string;
  entityDisplayName: string;
  year: number;
  taxReviewItems: TaxReviewItem[];
};

export function YearView({
  pkg,
  workspaceId,
  workspaceSlug,
  entityId,
  entityDisplayName,
  year,
  taxReviewItems
}: YearViewProps) {
  const [activeTab, setActiveTab] = useState<Tab>("manual");

  const pendingCount = taxReviewItems.filter((it) => it.status === "pending").length;

  const tabs: { id: Tab; label: string; badge?: number }[] = [
    { id: "manual", label: "Manual" },
    {
      id: "specialist",
      label: "Specialist draft",
      badge: pendingCount > 0 ? pendingCount : undefined
    }
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
    </div>
  );
}
