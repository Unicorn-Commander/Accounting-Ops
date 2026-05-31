"use client";

/**
 * ClusterReview — the orchestrator for the cluster-card UX.
 *
 * Owns:
 *   - rendering the cluster cards (top) + the singleton swipe deck (bottom)
 *   - the running "session progress" badge in the header
 *   - the final summary card once enough work has been done
 *   - the undo toast for both flows
 *
 * Session persistence: we don't need explicit state — every successful
 * decision flips the underlying approval_queue.status to approved/rejected,
 * and the next page fetch will re-derive what's left. Skipped items are
 * tracked locally only (they're "decide later", not a server state).
 *
 * Render strategy: cluster cards above singleton deck. On mobile both flow
 * vertically; on lg+ they sit side by side (cards left, deck right).
 */

import { useState, useMemo, useCallback } from "react";
import Link from "next/link";
import {
  Layers,
  ArrowRight,
  Sparkles,
  CheckCircle2,
  Sigma,
  Flag
} from "lucide-react";
import { cn } from "@/lib/cn";
import {
  type ApprovalClustersResponse,
  type ApprovalItem,
  formatUsd
} from "@/lib/approval-clusters";
import { ClusterCard } from "./cluster-card";
import { SingletonDeck } from "./singleton-deck";
import { useUndoToast } from "./use-undo-toast";

// -----------------------------------------------------------------------
// Filter pill
// -----------------------------------------------------------------------

function FilterChip({
  active,
  onClick,
  label,
  count,
  tone
}: {
  active: boolean;
  onClick: () => void;
  label: string;
  count: number;
  tone: "credit" | "pending" | "debit" | "brand";
}) {
  const activeMap = {
    credit: "border-credit/50 bg-credit/15 text-credit",
    pending: "border-pending/50 bg-pending/15 text-pending",
    debit: "border-debit/50 bg-debit/15 text-debit",
    brand: "border-brand-500/50 bg-brand-500/15 text-brand-300"
  };
  return (
    <button
      type="button"
      onClick={onClick}
      className={cn(
        "inline-flex h-8 items-center gap-1.5 rounded-full border px-3 text-xs",
        active
          ? activeMap[tone]
          : "border-subtle bg-elevated text-ink-secondary hover:bg-surface"
      )}
    >
      {label}
      <span
        className={cn(
          "rounded-full px-1.5 py-0.5 font-mono text-[10px]",
          active ? "bg-surface/40" : "bg-white/5 text-ink-tertiary"
        )}
      >
        {count}
      </span>
    </button>
  );
}

// -----------------------------------------------------------------------
// Summary strip
// -----------------------------------------------------------------------

function SummaryStrip({
  data,
  sessionApproved,
  sessionRejected,
  sessionSkipped
}: {
  data: ApprovalClustersResponse;
  sessionApproved: number;
  sessionRejected: number;
  sessionSkipped: number;
}) {
  const remaining = Math.max(
    data.totals.approvable_pending - sessionApproved - sessionRejected,
    0
  );
  const sessionTotal = sessionApproved + sessionRejected + sessionSkipped;

  return (
    <div className="flex flex-wrap items-center gap-3 rounded-2xl border border-default bg-elevated p-4">
      <div className="min-w-0">
        <p className="text-[10px] uppercase tracking-wider text-ink-tertiary">
          Pending review
        </p>
        <p className="mt-1 font-mono text-xl font-semibold text-ink-primary">
          {data.totals.approvable_pending}
        </p>
      </div>
      <div className="text-ink-tertiary">·</div>
      <div className="min-w-0">
        <p className="text-[10px] uppercase tracking-wider text-ink-tertiary">
          Clusters
        </p>
        <p className="mt-1 font-mono text-xl font-semibold text-ink-primary">
          {data.clusters.length}
        </p>
      </div>
      <div className="text-ink-tertiary">·</div>
      <div className="min-w-0">
        <p className="text-[10px] uppercase tracking-wider text-ink-tertiary">
          Singletons
        </p>
        <p className="mt-1 font-mono text-xl font-semibold text-ink-primary">
          {data.singletons.length}
        </p>
      </div>
      {data.totals.flagged_pending > 0 && (
        <>
          <div className="text-ink-tertiary">·</div>
          <div className="min-w-0">
            <p className="text-[10px] uppercase tracking-wider text-ink-tertiary">
              Flagged
            </p>
            <p className="mt-1 font-mono text-xl font-semibold text-pending">
              {data.totals.flagged_pending}
            </p>
          </div>
        </>
      )}

      {sessionTotal > 0 && (
        <div className="ml-auto rounded-lg border border-credit/40 bg-credit/10 px-3 py-2 text-xs">
          <p className="text-[10px] uppercase tracking-wider text-credit">
            This session
          </p>
          <p className="mt-1 flex items-center gap-2 font-mono text-ink-primary">
            <span className="text-credit">{sessionApproved}✓</span>
            {sessionRejected > 0 && (
              <span className="text-debit">{sessionRejected}✗</span>
            )}
            {sessionSkipped > 0 && (
              <span className="text-pending">{sessionSkipped}⤳</span>
            )}
            <span className="text-ink-tertiary">/ {remaining} left</span>
          </p>
        </div>
      )}
    </div>
  );
}

// -----------------------------------------------------------------------
// Final summary card (appears once the session has decisions)
// -----------------------------------------------------------------------

function FinalSummary({
  approved,
  rejected,
  skipped,
  approvedUsd,
  flaggedRemaining
}: {
  approved: number;
  rejected: number;
  skipped: number;
  approvedUsd: number;
  flaggedRemaining: number;
}) {
  if (approved + rejected + skipped === 0) return null;

  return (
    <div className="rounded-2xl border-2 border-credit/40 bg-credit/[0.04] p-5">
      <div className="flex items-center gap-2">
        <CheckCircle2 size={18} className="text-credit" />
        <h3 className="text-base font-semibold text-ink-primary">
          Session progress
        </h3>
      </div>
      <dl className="mt-3 grid grid-cols-2 gap-3 text-sm sm:grid-cols-4">
        <div>
          <dt className="text-[10px] uppercase tracking-wider text-ink-tertiary">
            Approved
          </dt>
          <dd className="mt-0.5 font-mono text-xl font-semibold text-credit">
            {approved}
          </dd>
        </div>
        {rejected > 0 && (
          <div>
            <dt className="text-[10px] uppercase tracking-wider text-ink-tertiary">
              Rejected
            </dt>
            <dd className="mt-0.5 font-mono text-xl font-semibold text-debit">
              {rejected}
            </dd>
          </div>
        )}
        {skipped > 0 && (
          <div>
            <dt className="text-[10px] uppercase tracking-wider text-ink-tertiary">
              Skipped
            </dt>
            <dd className="mt-0.5 font-mono text-xl font-semibold text-pending">
              {skipped}
            </dd>
          </div>
        )}
        <div>
          <dt className="text-[10px] uppercase tracking-wider text-ink-tertiary">
            $ approved
          </dt>
          <dd className="mt-0.5 font-mono text-xl font-semibold text-ink-primary">
            {formatUsd(approvedUsd)}
          </dd>
        </div>
      </dl>
      <p className="mt-3 text-xs text-ink-secondary">
        {approved} items posted to the journal.
        {flaggedRemaining > 0 && (
          <>
            {" "}
            {flaggedRemaining} flagged item{flaggedRemaining !== 1 ? "s" : ""}{" "}
            still need a CPA-eye review.
          </>
        )}
      </p>
      <div className="mt-3 flex flex-wrap items-center gap-3">
        <Link
          href="/reports/trial-balance"
          className="inline-flex h-9 items-center gap-1.5 rounded-lg bg-brand-500/15 px-3 text-sm font-medium text-brand-300 hover:bg-brand-500/25"
        >
          See trial balance
          <ArrowRight size={13} />
        </Link>
        {/* Audit quick-win #3: link to the tax dashboard so the user can
            pick the right year, rather than a hard-coded 2023 path that
            only matched Aaron's specific catch-up situation. */}
        <Link
          href="/taxes"
          className="inline-flex h-9 items-center gap-1.5 rounded-lg border border-subtle bg-elevated px-3 text-sm text-ink-secondary hover:bg-surface focus:outline-none focus-visible:ring-2 focus-visible:ring-brand-500 focus-visible:ring-offset-2 focus-visible:ring-offset-canvas"
        >
          Continue tax work
        </Link>
      </div>
    </div>
  );
}

// -----------------------------------------------------------------------
// Main component
// -----------------------------------------------------------------------

type ConfFilter = "all" | "high" | "medium" | "low";

export function ClusterReview({
  data,
  categories,
  workspaceId,
  canDecide
}: {
  data: ApprovalClustersResponse;
  categories: string[];
  workspaceId: string;
  canDecide: boolean;
}) {
  const [filter, setFilter] = useState<ConfFilter>("all");
  const [decidedClusters, setDecidedClusters] = useState<Set<string>>(new Set());
  const [sessionApproved, setSessionApproved] = useState(0);
  const [sessionRejected, setSessionRejected] = useState(0);
  const [sessionSkipped, setSessionSkipped] = useState(0);
  const [sessionApprovedUsd, setSessionApprovedUsd] = useState(0);
  const { showToast, ToastView } = useUndoToast();

  // ---- Cluster-card events ----
  const onClusterDecided = useCallback(
    (clusterId: string, decision: "approve" | "reject" | "skip", count: number) => {
      setDecidedClusters((prev) => new Set(prev).add(clusterId));
      if (decision === "approve") {
        setSessionApproved((n) => n + count);
        // Approximate $ — we know cluster total from the data.
        const cluster = data.clusters.find((c) => c.cluster_id === clusterId);
        if (cluster) {
          setSessionApprovedUsd((u) => u + cluster.total_amount_usd);
        }
        showToast(`Approved ${count} · ${clusterId.split("|")[0]}`, "approve");
      } else if (decision === "reject") {
        setSessionRejected((n) => n + count);
        showToast(`Rejected ${count}`, "reject");
      } else {
        setSessionSkipped((n) => n + count);
        showToast(`Skipped ${count} — decide later`, "skip");
      }
    },
    [data.clusters, showToast]
  );

  // ---- Singleton events ----
  const onSingletonDecided = useCallback(
    (item: ApprovalItem, decision: "approve" | "reject" | "skip") => {
      if (decision === "approve") {
        setSessionApproved((n) => n + 1);
        setSessionApprovedUsd((u) => u + Math.abs(item.amount_usd));
        showToast(`Approved ${item.vendor || "—"}`, "approve");
      } else if (decision === "reject") {
        setSessionRejected((n) => n + 1);
        showToast(`Rejected ${item.vendor || "—"}`, "reject");
      } else {
        setSessionSkipped((n) => n + 1);
      }
    },
    [showToast]
  );

  // ---- Visible clusters (filtered + decided hidden) ----
  const visibleClusters = useMemo(() => {
    return data.clusters
      .filter((c) => !decidedClusters.has(c.cluster_id))
      .filter((c) => filter === "all" || c.confidence === filter);
  }, [data.clusters, decidedClusters, filter]);

  // ---- Filter counts ----
  const filterCounts = useMemo(() => {
    const remaining = data.clusters.filter((c) => !decidedClusters.has(c.cluster_id));
    return {
      all: remaining.length,
      high: remaining.filter((c) => c.confidence === "high").length,
      medium: remaining.filter((c) => c.confidence === "medium").length,
      low: remaining.filter((c) => c.confidence === "low").length
    };
  }, [data.clusters, decidedClusters]);

  // Empty case after cluster review (all clusters decided AND no singletons).
  const allClustersDone = visibleClusters.length === 0;

  return (
    <div className="flex flex-col gap-5">
      {/* Summary */}
      <SummaryStrip
        data={data}
        sessionApproved={sessionApproved}
        sessionRejected={sessionRejected}
        sessionSkipped={sessionSkipped}
      />

      {/* Final summary card (only shows after any session activity) */}
      <FinalSummary
        approved={sessionApproved}
        rejected={sessionRejected}
        skipped={sessionSkipped}
        approvedUsd={sessionApprovedUsd}
        flaggedRemaining={data.totals.flagged_pending}
      />

      {/* Cluster section */}
      {data.clusters.length > 0 && (
        <section className="flex flex-col gap-3">
          <div className="flex flex-wrap items-center gap-3">
            <div className="flex items-center gap-2">
              <Layers size={15} className="text-brand-400" />
              <h2 className="text-base font-semibold text-ink-primary">
                Cluster cards
              </h2>
            </div>
            <span className="text-xs text-ink-tertiary">
              Bulk-approve similar transactions
            </span>
            <div className="ml-auto flex flex-wrap items-center gap-1.5">
              <FilterChip
                active={filter === "all"}
                onClick={() => setFilter("all")}
                label="All"
                count={filterCounts.all}
                tone="brand"
              />
              <FilterChip
                active={filter === "high"}
                onClick={() => setFilter("high")}
                label="High"
                count={filterCounts.high}
                tone="credit"
              />
              <FilterChip
                active={filter === "medium"}
                onClick={() => setFilter("medium")}
                label="Med"
                count={filterCounts.medium}
                tone="pending"
              />
              <FilterChip
                active={filter === "low"}
                onClick={() => setFilter("low")}
                label="Low"
                count={filterCounts.low}
                tone="debit"
              />
            </div>
          </div>

          {allClustersDone ? (
            <div className="rounded-2xl border border-default bg-surface px-6 py-10 text-center">
              <CheckCircle2 size={26} className="mx-auto mb-2 text-credit" />
              <p className="text-sm font-medium text-ink-primary">
                All clusters cleared{filter !== "all" ? ` at "${filter}" confidence` : ""}.
              </p>
              {filter !== "all" && (
                <button
                  type="button"
                  onClick={() => setFilter("all")}
                  className="mt-2 text-xs text-brand-400 underline-offset-2 hover:underline"
                >
                  Show all clusters
                </button>
              )}
            </div>
          ) : (
            <div className="grid grid-cols-1 gap-3 xl:grid-cols-2">
              {visibleClusters.map((cluster) => (
                <ClusterCard
                  key={cluster.cluster_id}
                  cluster={cluster}
                  workspaceId={workspaceId}
                  categories={categories}
                  canDecide={canDecide}
                  onDecided={(decision, count) =>
                    onClusterDecided(cluster.cluster_id, decision, count)
                  }
                />
              ))}
            </div>
          )}
        </section>
      )}

      {/* Singleton swipe deck */}
      {data.singletons.length > 0 && (
        <section className="flex flex-col gap-3">
          <div className="flex flex-wrap items-center gap-2">
            <Sparkles size={15} className="text-brand-400" />
            <h2 className="text-base font-semibold text-ink-primary">
              One-at-a-time
            </h2>
            <span className="text-xs text-ink-tertiary">
              Items that didn&apos;t fit a cluster
            </span>
          </div>
          <SingletonDeck
            items={data.singletons}
            categories={categories}
            workspaceId={workspaceId}
            canDecide={canDecide}
            onDecidedOne={onSingletonDecided}
          />
        </section>
      )}

      {/* Flagged-only nudge */}
      {data.totals.approvable_pending === 0 && data.totals.flagged_pending > 0 && (
        <div className="rounded-2xl border border-pending/40 bg-pending/[0.06] p-5 text-sm text-pending">
          <p className="flex items-center gap-2 font-medium">
            <Flag size={14} />
            All bulk-approvable items handled. {data.totals.flagged_pending}{" "}
            flagged item{data.totals.flagged_pending !== 1 ? "s" : ""} remain —
            each needs an individual decision.
          </p>
        </div>
      )}

      {/* Caught-up state */}
      {data.totals.total_pending === 0 && (
        <div className="rounded-2xl border border-default bg-surface px-6 py-16 text-center">
          <CheckCircle2 size={32} className="mx-auto mb-3 text-credit" />
          <p className="text-lg font-medium text-ink-primary">
            Inbox zero — books are caught up.
          </p>
          <p className="mt-1 text-sm text-ink-tertiary">
            No pending approvals for this entity right now.
          </p>
          <Link
            href="/reports/trial-balance"
            className="mt-4 inline-flex h-9 items-center gap-1.5 rounded-lg bg-brand-500/15 px-3 text-sm font-medium text-brand-300 hover:bg-brand-500/25"
          >
            <Sigma size={13} />
            View trial balance
          </Link>
        </div>
      )}

      {ToastView}
    </div>
  );
}
