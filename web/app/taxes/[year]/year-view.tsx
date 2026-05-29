"use client";

// YearView — three-tab wrapper for the /taxes/[year] page.
//
// Tab "Manual"          (default) — the existing Form1120View review surface.
// Tab "Specialist draft"          — the SpecialistDraftView for the AI queue.
// Tab "Run history"               — the RunHistoryView MLOps observability table.
//
// Tabs use local state so navigating between them is instant (no server round-trip).
// The active tab is NOT reflected in the URL — purely a UI concern within /taxes/[year].

import { useState } from "react";
import { Download, Loader2 } from "lucide-react";
import { cn } from "@/lib/cn";
import type { Form1120Package } from "@/lib/taxes-1120";
import type { TaxReviewItem } from "@/lib/tax-review-queue";
import type { SpecialistRun } from "@/lib/specialist-runs";
import { Form1120View } from "./form-1120-view";
import { SpecialistDraftView } from "./specialist-draft-view";
import { RunHistoryView } from "./run-history-view";

type Tab = "manual" | "specialist" | "runs";

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

// ---------------------------------------------------------------------------
// HandoffDownloadButton — downloads the full CPA Handoff Package ZIP.
// Uses a client-side fetch so it can stream the binary response through the
// Next.js proxy route with the user's session cookie attached automatically.
// ---------------------------------------------------------------------------

function HandoffDownloadButton({
  workspaceId,
  entityId,
  year,
}: {
  workspaceId: string;
  entityId: string;
  year: number;
}) {
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);

  async function handleDownload() {
    setBusy(true);
    setErr(null);
    try {
      const url = `/api/workspaces/${workspaceId}/entities/${entityId}/tax-drafts/${year}/handoff-package.zip`;
      const resp = await fetch(url, { cache: "no-store" });
      if (!resp.ok) {
        setErr(`Download failed (${resp.status}). Is the app stack running?`);
        return;
      }
      const blob = await resp.blob();
      const disposition = resp.headers.get("content-disposition") ?? "";
      const match = disposition.match(/filename="?([^"]+)"?/);
      const filename = match?.[1] ?? `handoff-${year}.zip`;
      const link = document.createElement("a");
      link.href = URL.createObjectURL(blob);
      link.download = filename;
      link.click();
      URL.revokeObjectURL(link.href);
    } catch {
      setErr("Network error — please try again.");
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="flex flex-col items-end gap-1">
      <button
        type="button"
        onClick={handleDownload}
        disabled={busy}
        className={cn(
          "inline-flex items-center gap-2 rounded-md px-4 py-2 text-sm font-semibold",
          "bg-brand-500 text-white shadow-sm",
          "hover:bg-brand-600 active:bg-brand-700",
          "disabled:cursor-not-allowed disabled:bg-elevated disabled:text-ink-disabled",
          "transition-colors"
        )}
      >
        {busy ? (
          <Loader2 size={15} className="animate-spin" />
        ) : (
          <Download size={15} />
        )}
        {busy ? "Building package…" : "Download Handoff Package"}
      </button>
      {err && (
        <p className="text-[11px] text-debit">{err}</p>
      )}
    </div>
  );
}

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
    }
  ];

  return (
    <div>
      {/* Tab bar + Download Handoff Package button */}
      <div className="mb-6 flex flex-wrap items-center justify-between gap-3">
        <div className="flex items-center gap-1.5">
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

        {/* Only show the handoff button when an entity (not consolidated) is selected */}
        {entityId !== "all" && (
          <HandoffDownloadButton
            workspaceId={workspaceId}
            entityId={entityId}
            year={year}
          />
        )}
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
          entityId={entityId}
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
    </div>
  );
}
