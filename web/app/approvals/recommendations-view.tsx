"use client";

/**
 * RecommendationsView
 *
 * The "AI Recommendations" sub-tab under /approvals.
 *
 * Shows cluster cards produced by the batch pre-analyzer
 * (analyze_pending_approvals MCP tool).  Each cluster card shows:
 *   - Vendor/category label
 *   - Count of rows in cluster
 *   - Total $ amount
 *   - AI Confidence badge (HIGH / MED / LOW)
 *   - Recommended decision
 *   - Rationale excerpt
 *   - [Approve all N]  button (POSTs to the existing batch-approve flow)
 *   - [Flag for review] toggle (marks rows as "needs human eyes" locally)
 *
 * "Approve all" fans out in parallel to the existing /decide endpoint —
 * identical to the batch approve logic in BankTxnBatchPanel.
 *
 * This component does NOT call the analyzer. The analyzer is an MCP tool
 * the accountant calls separately. This component just renders what is
 * already in the recommendation table.
 */

import { useRouter } from "next/navigation";
import { useState, useMemo, useTransition } from "react";
import {
  Check,
  Flag,
  Loader2,
  AlertTriangle,
  ChevronDown,
  ChevronUp,
  Sparkles
} from "lucide-react";
import { cn } from "@/lib/cn";
import { StatusChip } from "@/components/primitives";
import type {
  ApprovalRecommendation,
  RecommendationCluster,
  RecommendationConfidence
} from "@/lib/approval-recommendations";
import {
  clusterApprovalIds,
  clusterTotalCents
} from "@/lib/approval-recommendations";

// -----------------------------------------------------------------------
// Confidence badge
// -----------------------------------------------------------------------

function ConfidenceBadge({ c }: { c: RecommendationConfidence }) {
  const map: Record<RecommendationConfidence, string> = {
    high: "bg-credit/15 text-credit",
    medium: "bg-pending/15 text-pending",
    low: "bg-debit/15 text-debit"
  };
  const label: Record<RecommendationConfidence, string> = {
    high: "AI HIGH",
    medium: "AI MED",
    low: "AI LOW"
  };
  return (
    <span
      className={cn(
        "inline-flex items-center gap-1 rounded-full px-2 py-0.5 text-[10px] font-medium uppercase tracking-wider",
        map[c]
      )}
    >
      <Sparkles size={9} />
      {label[c]}
    </span>
  );
}

// -----------------------------------------------------------------------
// Cluster card
// -----------------------------------------------------------------------

function ClusterCard({
  cluster,
  recommendations,
  workspaceId,
  canDecide
}: {
  cluster: RecommendationCluster;
  recommendations: ApprovalRecommendation[];
  workspaceId: string;
  canDecide: boolean;
}) {
  const router = useRouter();
  const [expanded, setExpanded] = useState(false);
  const [flagged, setFlagged] = useState(false);
  const [approving, startApproveTransition] = useTransition();
  const [progress, setProgress] = useState<{ done: number; total: number; errors: string[] } | null>(null);
  const [approved, setApproved] = useState(false);

  const approvalIds = useMemo(
    () => clusterApprovalIds(cluster.cluster_id, recommendations),
    [cluster.cluster_id, recommendations]
  );
  const totalCents = useMemo(
    () => clusterTotalCents(cluster.cluster_id, recommendations),
    [cluster.cluster_id, recommendations]
  );

  // Sample rows for the expanded view
  const sampleRows = useMemo(
    () =>
      recommendations
        .filter((r) => r.cluster_id === cluster.cluster_id)
        .slice(0, 6),
    [cluster.cluster_id, recommendations]
  );

  // Representative category
  const category = cluster.representative_category ?? "Unknown category";

  function formatCents(cents: number): string {
    if (cents === 0) return "$0";
    return new Intl.NumberFormat("en-US", {
      style: "currency",
      currency: "USD",
      maximumFractionDigits: 0
    }).format(cents / 100);
  }

  function handleApproveAll() {
    if (!canDecide || approvalIds.length === 0) return;
    startApproveTransition(async () => {
      const errors: string[] = [];
      let done = 0;
      setProgress({ done: 0, total: approvalIds.length, errors: [] });

      const CONCURRENCY = 10;
      for (let i = 0; i < approvalIds.length; i += CONCURRENCY) {
        const chunk = approvalIds.slice(i, i + CONCURRENCY);
        await Promise.all(
          chunk.map(async (id) => {
            try {
              const res = await fetch(
                `/api/workspaces/${workspaceId}/approvals/${id}/decide`,
                {
                  method: "POST",
                  headers: { "Content-Type": "application/json" },
                  body: JSON.stringify({ decision: "approve" })
                }
              );
              if (!res.ok) {
                const j = await res.json().catch(() => ({})) as { detail?: string };
                errors.push(`${id}: ${j.detail ?? res.status}`);
              } else {
                done++;
              }
            } catch (e) {
              errors.push(`${id}: ${e instanceof Error ? e.message : "error"}`);
            }
          })
        );
        setProgress({ done, total: approvalIds.length, errors: [...errors] });
      }
      setProgress(null);
      if (errors.length === 0) setApproved(true);
      router.refresh();
    });
  }

  if (approved) {
    return (
      <div className="rounded-lg border border-credit/30 bg-credit/[0.04] px-5 py-4">
        <p className="flex items-center gap-2 text-sm text-credit">
          <Check size={14} />
          {cluster.count} rows approved — ledger updated.
        </p>
      </div>
    );
  }

  return (
    <div
      className={cn(
        "rounded-lg border bg-surface transition-colors",
        flagged ? "border-pending/50 bg-pending/[0.03]" : "border-default"
      )}
    >
      {/* Card header */}
      <div className="flex flex-col gap-3 p-4 sm:flex-row sm:items-start sm:gap-4">
        {/* Left: info */}
        <div className="min-w-0 flex-1">
          <div className="flex flex-wrap items-center gap-2">
            <ConfidenceBadge c={cluster.confidence} />
            {flagged && (
              <StatusChip tone="pending">Flagged for review</StatusChip>
            )}
            <span className="font-semibold text-ink-primary">{category}</span>
          </div>

          {/* Audit quick-win #2: hide raw cluster_id from the primary line
              — surface via title-tooltip only. */}
          <div
            className="mt-2 flex flex-wrap items-center gap-3 text-sm text-ink-secondary"
            title={`Cluster ID: ${cluster.cluster_id}`}
          >
            <span className="font-mono font-medium text-ink-primary">
              {formatCents(totalCents)}
            </span>
            <span className="text-ink-tertiary">·</span>
            <span>{cluster.count} transaction{cluster.count !== 1 ? "s" : ""}</span>
          </div>

          {/* Rationale excerpt (first sample row) */}
          {sampleRows[0]?.rationale && (
            <p className="mt-2 text-xs leading-relaxed text-ink-secondary">
              {sampleRows[0].rationale}
            </p>
          )}
        </div>

        {/* Right: actions */}
        {canDecide && !flagged && (
          <div className="flex shrink-0 flex-wrap items-center gap-2">
            {cluster.confidence !== "low" && (
              <button
                onClick={handleApproveAll}
                disabled={approving || approvalIds.length === 0}
                className={cn(
                  "inline-flex h-8 items-center gap-1.5 rounded-md px-3 text-xs font-medium disabled:opacity-50",
                  "bg-credit/15 text-credit hover:bg-credit/25"
                )}
              >
                {approving ? (
                  <>
                    <Loader2 size={12} className="animate-spin" />
                    {progress
                      ? `${progress.done}/${progress.total}`
                      : "Approving…"}
                  </>
                ) : (
                  <>
                    <Check size={12} />
                    Approve all {cluster.count}
                  </>
                )}
              </button>
            )}
            <button
              onClick={() => setFlagged(true)}
              className="inline-flex h-8 items-center gap-1.5 rounded-md border border-subtle bg-surface px-3 text-xs text-ink-secondary hover:bg-elevated"
            >
              <Flag size={11} />
              Flag for review
            </button>
          </div>
        )}

        {/* LOW confidence: always show flag, never show approve */}
        {canDecide && !flagged && cluster.confidence === "low" && (
          <div className="shrink-0">
            <button
              onClick={() => setFlagged(true)}
              className="inline-flex h-8 items-center gap-1.5 rounded-md border border-debit/40 bg-debit/[0.06] px-3 text-xs text-debit hover:bg-debit/15"
            >
              <AlertTriangle size={11} />
              Needs CPA review
            </button>
          </div>
        )}
      </div>

      {/* Progress bar */}
      {approving && progress && (
        <div className="px-4 pb-3">
          <div className="h-1 w-full overflow-hidden rounded-full bg-white/5">
            <div
              className="h-full bg-credit transition-all"
              style={{
                width: `${(progress.done / progress.total) * 100}%`
              }}
            />
          </div>
          {progress.errors.length > 0 && (
            <p className="mt-1 text-[10px] text-debit">
              {progress.errors.length} error{progress.errors.length !== 1 ? "s" : ""}
            </p>
          )}
        </div>
      )}

      {/* Expand / collapse sample rows */}
      <button
        onClick={() => setExpanded((v) => !v)}
        className="flex w-full items-center gap-1 border-t border-default px-4 py-2 text-[11px] text-ink-tertiary hover:bg-elevated"
      >
        {expanded ? <ChevronUp size={11} /> : <ChevronDown size={11} />}
        {expanded ? "Hide" : "View"} sample rows
      </button>

      {expanded && (
        <div className="divide-y divide-default">
          {sampleRows.map((r) => (
            <div
              key={r.recommendation_id}
              className="flex flex-wrap items-center gap-x-4 gap-y-1 px-4 py-2 text-xs"
            >
              <span className="font-mono text-ink-tertiary">
                {(r.payload as { effective_date?: string } | null)?.effective_date ??
                  r.approval_created_at.slice(0, 10)}
              </span>
              <span className="text-ink-secondary">
                {(r.payload as { payee?: string } | null)?.payee ?? "—"}
              </span>
              <span className="font-mono text-ink-primary">
                {r.amount
                  ? new Intl.NumberFormat("en-US", {
                      style: "currency",
                      currency: r.currency_code ?? "USD"
                    }).format(Number(r.amount))
                  : "—"}
              </span>
              <ConfidenceBadge c={r.confidence} />
            </div>
          ))}
          {cluster.count > sampleRows.length && (
            <p className="px-4 py-2 text-[11px] text-ink-tertiary">
              + {cluster.count - sampleRows.length} more in this cluster
            </p>
          )}
        </div>
      )}
    </div>
  );
}

// -----------------------------------------------------------------------
// Summary strip
// -----------------------------------------------------------------------

function SummaryStrip({
  clusters,
  recommendations
}: {
  clusters: RecommendationCluster[];
  recommendations: ApprovalRecommendation[];
}) {
  const high = clusters.filter((c) => c.confidence === "high").length;
  const med = clusters.filter((c) => c.confidence === "medium").length;
  const low = clusters.filter((c) => c.confidence === "low").length;
  const totalCount = recommendations.length;

  const highCents = clusters
    .filter((c) => c.confidence === "high")
    .reduce((s, c) => s + clusterTotalCents(c.cluster_id, recommendations), 0);

  function formatCents(cents: number): string {
    return new Intl.NumberFormat("en-US", {
      style: "currency",
      currency: "USD",
      maximumFractionDigits: 0
    }).format(cents / 100);
  }

  return (
    <div className="flex flex-wrap gap-3 rounded-lg border border-default bg-elevated px-5 py-3">
      <div className="flex items-center gap-2 text-xs">
        <span className="font-medium text-ink-primary">{totalCount}</span>
        <span className="text-ink-secondary">transactions analyzed</span>
      </div>
      <span className="text-ink-tertiary hidden sm:block">·</span>
      <div className="flex items-center gap-1.5 text-xs">
        <span className="inline-flex items-center rounded-full bg-credit/15 px-2 py-0.5 text-[10px] font-medium text-credit">
          {high} HIGH
        </span>
        <span className="inline-flex items-center rounded-full bg-pending/15 px-2 py-0.5 text-[10px] font-medium text-pending">
          {med} MED
        </span>
        <span className="inline-flex items-center rounded-full bg-debit/15 px-2 py-0.5 text-[10px] font-medium text-debit">
          {low} LOW
        </span>
      </div>
      {highCents > 0 && (
        <>
          <span className="text-ink-tertiary hidden sm:block">·</span>
          <div className="text-xs text-ink-secondary">
            <span className="font-mono font-medium text-credit">
              {formatCents(highCents)}
            </span>{" "}
            ready to approve
          </div>
        </>
      )}
    </div>
  );
}

// -----------------------------------------------------------------------
// Main export
// -----------------------------------------------------------------------

export function RecommendationsView({
  recommendations,
  clusters,
  workspaceId,
  canDecide
}: {
  recommendations: ApprovalRecommendation[];
  clusters: RecommendationCluster[];
  workspaceId: string;
  canDecide: boolean;
}) {
  const [filterConf, setFilterConf] = useState<"all" | RecommendationConfidence>(
    "all"
  );

  const filteredClusters = useMemo(() => {
    if (filterConf === "all") return clusters;
    return clusters.filter((c) => c.confidence === filterConf);
  }, [clusters, filterConf]);

  if (recommendations.length === 0) {
    return (
      <div className="rounded-lg border border-default bg-surface px-5 py-10 text-center">
        <Sparkles size={24} className="mx-auto mb-3 text-ink-tertiary" />
        <p className="text-sm font-medium text-ink-secondary">
          No recommendations yet
        </p>
        <p className="mt-1 text-xs text-ink-tertiary">
          Run{" "}
          <span className="font-mono">analyze_pending_approvals</span> via the
          MCP tool to generate cluster recommendations for the pending
          transactions.
        </p>
      </div>
    );
  }

  return (
    <div className="flex flex-col gap-4">
      {/* Summary strip */}
      <SummaryStrip clusters={clusters} recommendations={recommendations} />

      {/* Confidence filter */}
      <div className="flex items-center gap-2">
        <span className="text-[11px] uppercase tracking-wider text-ink-tertiary">
          Filter
        </span>
        {(["all", "high", "medium", "low"] as const).map((v) => (
          <button
            key={v}
            onClick={() => setFilterConf(v)}
            className={cn(
              "inline-flex h-7 items-center rounded-md border px-3 text-xs",
              filterConf === v
                ? v === "high"
                  ? "border-credit/40 bg-credit/10 text-credit"
                  : v === "medium"
                    ? "border-pending/40 bg-pending/10 text-pending"
                    : v === "low"
                      ? "border-debit/40 bg-debit/10 text-debit"
                      : "border-brand-500/40 bg-brand-500/10 text-brand-300"
                : "border-subtle bg-surface text-ink-secondary hover:bg-elevated"
            )}
          >
            {v === "all" ? "All" : v.charAt(0).toUpperCase() + v.slice(1)}
            {v !== "all" && (
              <span className="ml-1.5 font-mono text-[10px]">
                ({clusters.filter((c) => c.confidence === v).length})
              </span>
            )}
          </button>
        ))}
      </div>

      {/* Cluster cards */}
      <div className="flex flex-col gap-3">
        {filteredClusters.length === 0 ? (
          <div className="rounded-lg border border-default bg-surface px-5 py-6 text-center text-sm text-ink-secondary">
            No clusters match this filter.
          </div>
        ) : (
          filteredClusters.map((cluster) => (
            <ClusterCard
              key={cluster.cluster_id}
              cluster={cluster}
              recommendations={recommendations}
              workspaceId={workspaceId}
              canDecide={canDecide}
            />
          ))
        )}
      </div>

      {!canDecide && (
        <div className="rounded-lg border border-pending/40 bg-pending/[0.06] px-5 py-3 text-xs text-ink-secondary">
          You can review these recommendations, but only an{" "}
          <strong>admin</strong> can approve clusters.
        </div>
      )}
    </div>
  );
}
