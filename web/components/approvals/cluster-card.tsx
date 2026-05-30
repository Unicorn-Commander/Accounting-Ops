"use client";

/**
 * ClusterCard
 *
 * One card for one cluster of similar pending approvals. Layout:
 *
 *   ┌─────────────────────────────────────────────────────────────┐
 *   │ [LOW]   venmo des payment                              50× │
 *   │         Suggested category: [Uncategorized       ▾]       │
 *   │         $13,766 total · $29–$176 range · 2022 → 2024       │
 *   │                                                            │
 *   │   ◦ 2024-11-12  $87.00  Venmo payment                     │
 *   │   ◦ 2024-10-08  $42.50  Venmo payment                     │
 *   │   ◦ ...                            [Show all 50 ▾]        │
 *   │                                                            │
 *   │   ⚠ 2 items flagged — must be reviewed individually        │
 *   │                                                            │
 *   │   [✓ Approve all 48]  [⤳ Skip]  [✗ Flag for review]       │
 *   └─────────────────────────────────────────────────────────────┘
 */

import { useState, useTransition, useMemo } from "react";
import { useRouter } from "next/navigation";
import {
  Check,
  ChevronDown,
  ChevronUp,
  Loader2,
  AlertTriangle,
  SkipForward,
  Flag,
  X
} from "lucide-react";
import { cn } from "@/lib/cn";
import {
  type ApprovalCluster,
  type ApprovalItem,
  type ClusterConfidence,
  formatUsd,
  formatUsdPrecise,
  confidenceLabel,
  flagLabel
} from "@/lib/approval-clusters";

// -----------------------------------------------------------------------
// Confidence pill (color-coded outline that wraps the whole card)
// -----------------------------------------------------------------------

function confidenceBorder(c: ClusterConfidence): string {
  return c === "high"
    ? "border-credit/40 hover:border-credit/60"
    : c === "medium"
      ? "border-pending/40 hover:border-pending/60"
      : "border-debit/40 hover:border-debit/60";
}

function ConfidenceBadge({ c }: { c: ClusterConfidence }) {
  const map: Record<ClusterConfidence, string> = {
    high: "bg-credit/20 text-credit",
    medium: "bg-pending/20 text-pending",
    low: "bg-debit/20 text-debit"
  };
  return (
    <span
      className={cn(
        "inline-flex items-center gap-1 rounded-full px-2 py-0.5 text-[10px] font-semibold uppercase tracking-wider",
        map[c]
      )}
    >
      {confidenceLabel(c)}
    </span>
  );
}

// -----------------------------------------------------------------------
// Item preview row (one of the up-to-5 samples)
// -----------------------------------------------------------------------

function ItemRow({ item }: { item: ApprovalItem }) {
  return (
    <li className="flex items-baseline gap-3 px-3 py-1.5 text-[12px]">
      <span className="w-[78px] shrink-0 font-mono text-ink-tertiary">
        {item.date ?? "—"}
      </span>
      <span className="w-[88px] shrink-0 text-right font-mono text-ink-primary">
        {formatUsdPrecise(item.amount_usd)}
      </span>
      <span className="min-w-0 flex-1 truncate text-ink-secondary" title={item.vendor}>
        {item.vendor || "—"}
      </span>
    </li>
  );
}

// -----------------------------------------------------------------------
// Main card
// -----------------------------------------------------------------------

export function ClusterCard({
  cluster,
  workspaceId,
  categories,
  canDecide,
  onDecided,
  onUndoFlag
}: {
  cluster: ApprovalCluster;
  workspaceId: string;
  /** Valid categories for the override dropdown (from /approvals/categories) */
  categories: string[];
  canDecide: boolean;
  /** Called after a successful bulk action so the parent can hide this card */
  onDecided: (kind: "approve" | "reject" | "skip", count: number) => void;
  /** Tells the parent to surface an undo toast with a re-flag affordance */
  onUndoFlag?: (clusterId: string) => void;
}) {
  const router = useRouter();
  const [expanded, setExpanded] = useState(false);
  const [showFlagged, setShowFlagged] = useState(false);
  const [overrideCategory, setOverrideCategory] = useState<string>(
    cluster.category_suggested
  );
  const [pending, startTransition] = useTransition();
  const [error, setError] = useState<string | null>(null);
  const [progress, setProgress] = useState<{
    done: number;
    total: number;
    errors: number;
  } | null>(null);
  const [decided, setDecided] = useState<"approve" | "reject" | "skip" | null>(
    null
  );

  // Categories that the dropdown can pick from. Always include the suggested
  // category even if it's not in the valid set (the backend will reject the
  // override if it's invalid; better UX than dropping the option silently).
  const categoryOptions = useMemo(() => {
    const opts = new Set<string>(categories);
    if (cluster.category_suggested) opts.add(cluster.category_suggested);
    return Array.from(opts).sort();
  }, [categories, cluster.category_suggested]);

  // -----------------------------------------------------------------------
  // Submit
  // -----------------------------------------------------------------------

  async function submit(decision: "approve" | "reject" | "skip") {
    if (!canDecide && decision !== "skip") return;
    setError(null);

    if (decision === "skip") {
      // Pure client-side; no API call. Parent hides the card.
      setDecided("skip");
      onDecided("skip", cluster.approvable_count);
      return;
    }

    startTransition(async () => {
      const total = cluster.approvable_count;
      setProgress({ done: 0, total, errors: 0 });

      // Server caps bulk-decide at 200 items / call. Chunk client-side.
      const CHUNK = 200;
      const slices: number[] = [];
      for (let i = 0; i < Math.max(total, 1); i += CHUNK) slices.push(i);

      let errors = 0;
      let done = 0;

      try {
        // If total <= CHUNK we can let the server resolve the cluster
        // (cleaner audit trail — "approved by cluster X"). For >200 we
        // fall back to client-resolved approval_ids since cluster_id
        // would resolve to >200 again.
        if (total <= CHUNK) {
          const res = await fetch(
            `/api/workspaces/${workspaceId}/approvals/bulk-decide`,
            {
              method: "POST",
              headers: { "Content-Type": "application/json" },
              body: JSON.stringify({
                cluster_id: cluster.cluster_id,
                decision,
                override_category:
                  decision === "approve" && overrideCategory ? overrideCategory : null,
                note: `Cluster decide via /approvals UX (${cluster.cluster_id})`
              })
            }
          );
          const body = (await res.json().catch(() => ({}))) as {
            ok?: boolean;
            approved?: number;
            rejected?: number;
            failed?: number;
            detail?: string;
            results?: Array<{ ok: boolean }>;
          };
          if (!res.ok) {
            throw new Error(body.detail ?? `HTTP ${res.status}`);
          }
          done = (body.approved ?? 0) + (body.rejected ?? 0);
          errors = body.failed ?? 0;
        } else {
          // Manual chunking: pull all approvable approval_ids out of the
          // (already-rendered) sample + remainder. The /clusters endpoint
          // gave us 5 items + remainder_count; we don't have ALL ids on the
          // client so we fall back to fetching by cluster_id in 200-row
          // batches. The server-side endpoint refuses >200 in one call, but
          // we can hit it K times with explicit approval_ids by pulling
          // the cluster fresh between calls (each successful batch removes
          // ~200 rows from pending, so the next cluster_id resolution
          // returns the next batch).
          while (done + errors < total) {
            const res = await fetch(
              `/api/workspaces/${workspaceId}/approvals/bulk-decide`,
              {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({
                  cluster_id: cluster.cluster_id,
                  decision,
                  override_category:
                    decision === "approve" && overrideCategory ? overrideCategory : null,
                  note: `Cluster decide via /approvals UX (chunked)`
                })
              }
            );
            const body = (await res.json().catch(() => ({}))) as {
              ok?: boolean;
              approved?: number;
              rejected?: number;
              failed?: number;
              detail?: string;
            };
            if (!res.ok) {
              throw new Error(body.detail ?? `HTTP ${res.status}`);
            }
            const batchOk = (body.approved ?? 0) + (body.rejected ?? 0);
            const batchErr = body.failed ?? 0;
            if (batchOk + batchErr === 0) break; // nothing left to do
            done += batchOk;
            errors += batchErr;
            setProgress({ done, total, errors });
          }
        }

        setProgress({ done, total, errors });
        setDecided(decision);
        onDecided(decision, done);

        // Refresh the server-rendered page so totals update.
        router.refresh();
      } catch (e) {
        setError(e instanceof Error ? e.message : "Unknown error");
        setProgress(null);
      }
    });
  }

  // -----------------------------------------------------------------------
  // Render
  // -----------------------------------------------------------------------

  if (decided) {
    const tone =
      decided === "approve"
        ? "border-credit/40 bg-credit/[0.06] text-credit"
        : decided === "reject"
          ? "border-debit/40 bg-debit/[0.06] text-debit"
          : "border-pending/40 bg-pending/[0.06] text-pending";
    const verb =
      decided === "approve"
        ? "Approved"
        : decided === "reject"
          ? "Rejected"
          : "Skipped";
    return (
      <div className={cn("rounded-xl border px-4 py-3", tone)}>
        <p className="flex items-center gap-2 text-sm">
          <Check size={14} />
          <span className="font-semibold">{cluster.vendor_signature}</span>
          <span>·</span>
          <span>
            {verb} {progress?.done ?? cluster.approvable_count} item
            {(progress?.done ?? cluster.approvable_count) !== 1 ? "s" : ""}
          </span>
          {progress && progress.errors > 0 && (
            <span className="text-debit">· {progress.errors} errors</span>
          )}
          {onUndoFlag && decided !== "skip" && (
            <button
              onClick={() => onUndoFlag(cluster.cluster_id)}
              className="ml-auto text-[11px] underline-offset-2 hover:underline"
            >
              View items
            </button>
          )}
        </p>
      </div>
    );
  }

  const dateLabel =
    cluster.date_range[0] && cluster.date_range[1]
      ? cluster.date_range[0] === cluster.date_range[1]
        ? cluster.date_range[0]
        : `${cluster.date_range[0]} → ${cluster.date_range[1]}`
      : "—";

  const [minAmt, maxAmt] = cluster.amount_range;
  const amtRangeLabel =
    minAmt === maxAmt
      ? formatUsdPrecise(minAmt)
      : `${formatUsdPrecise(minAmt)}–${formatUsdPrecise(maxAmt)}`;

  return (
    <div
      className={cn(
        "rounded-xl border bg-surface transition-colors",
        confidenceBorder(cluster.confidence)
      )}
    >
      {/* ---- Card body ---- */}
      <div className="flex flex-col gap-3 p-4 sm:p-5">
        {/* Top: vendor + count + confidence */}
        <div className="flex flex-wrap items-start gap-x-3 gap-y-1.5">
          <ConfidenceBadge c={cluster.confidence} />
          <div className="min-w-0 flex-1">
            <h3 className="truncate text-lg font-semibold tracking-tight text-ink-primary">
              {cluster.vendor_signature || "—"}
            </h3>
            <p className="mt-0.5 text-xs text-ink-tertiary">
              {cluster.count} transaction{cluster.count !== 1 ? "s" : ""} ·{" "}
              <span className="font-mono">{cluster.cluster_id}</span>
            </p>
          </div>
          <div className="shrink-0 text-right">
            <p className="font-mono text-2xl font-semibold text-ink-primary">
              {formatUsd(cluster.total_amount_usd)}
            </p>
            <p className="mt-0.5 text-[10px] uppercase tracking-wider text-ink-tertiary">
              total
            </p>
          </div>
        </div>

        {/* Metadata strip: range + dates */}
        <div className="flex flex-wrap items-center gap-x-4 gap-y-1 text-xs text-ink-secondary">
          <span>
            <span className="text-ink-tertiary">Range</span>{" "}
            <span className="font-mono">{amtRangeLabel}</span>
          </span>
          <span className="text-ink-tertiary">·</span>
          <span>
            <span className="text-ink-tertiary">Dates</span>{" "}
            <span className="font-mono">{dateLabel}</span>
          </span>
        </div>

        {/* Category override dropdown */}
        <div className="flex flex-wrap items-center gap-2 text-xs">
          <label className="text-ink-tertiary" htmlFor={`cat-${cluster.cluster_id}`}>
            Post to category
          </label>
          <select
            id={`cat-${cluster.cluster_id}`}
            value={overrideCategory}
            onChange={(e) => setOverrideCategory(e.target.value)}
            disabled={pending || !canDecide}
            className="h-8 min-w-[200px] rounded-md border border-subtle bg-elevated px-2 text-xs text-ink-primary focus:outline-none focus:ring-1 focus:ring-brand-500 disabled:opacity-50"
          >
            {categoryOptions.length === 0 && (
              <option value="">{cluster.category_suggested ?? "—"}</option>
            )}
            {categoryOptions.map((c) => (
              <option key={c} value={c}>
                {c}
              </option>
            ))}
          </select>
        </div>

        {/* Sample items + flagged callout */}
        <div className="rounded-lg border border-default bg-elevated">
          <ul className="divide-y divide-default">
            {(expanded ? cluster.items : cluster.items.slice(0, 3)).map((it) => (
              <ItemRow key={it.approval_id} item={it} />
            ))}
          </ul>
          {(cluster.items.length > 3 || cluster.remainder_count > 0) && (
            <button
              type="button"
              onClick={() => setExpanded((v) => !v)}
              className="flex w-full items-center justify-center gap-1 border-t border-default px-3 py-1.5 text-[11px] text-ink-tertiary hover:bg-surface"
            >
              {expanded ? <ChevronUp size={11} /> : <ChevronDown size={11} />}
              {expanded
                ? `Hide sample`
                : cluster.remainder_count > 0
                  ? `Show all ${cluster.count}`
                  : `Show all ${cluster.items.length}`}
            </button>
          )}
        </div>

        {/* Flagged items callout */}
        {cluster.flagged_count > 0 && (
          <div className="rounded-lg border border-pending/40 bg-pending/[0.05] px-3 py-2.5">
            <button
              type="button"
              onClick={() => setShowFlagged((v) => !v)}
              className="flex w-full items-center gap-2 text-[12px] text-pending"
            >
              <AlertTriangle size={13} />
              <span className="font-medium">
                {cluster.flagged_count} item{cluster.flagged_count !== 1 ? "s" : ""}{" "}
                flagged
              </span>
              <span className="text-ink-tertiary">— excluded from bulk approve</span>
              {showFlagged ? (
                <ChevronUp size={11} className="ml-auto" />
              ) : (
                <ChevronDown size={11} className="ml-auto" />
              )}
            </button>
            {showFlagged && (
              <ul className="mt-2 space-y-1">
                {cluster.flagged_items.map((it) => (
                  <li
                    key={it.approval_id}
                    className="flex items-baseline gap-2 text-[11px]"
                  >
                    <span className="font-mono text-ink-tertiary">{it.date ?? "—"}</span>
                    <span className="font-mono text-ink-primary">
                      {formatUsdPrecise(it.amount_usd)}
                    </span>
                    <span className="truncate text-ink-secondary">{it.vendor}</span>
                    <span className="ml-auto flex flex-wrap gap-1">
                      {it.flags.map((f) => (
                        <span
                          key={f}
                          className="rounded-full bg-pending/15 px-1.5 py-0.5 font-mono text-[9px] text-pending"
                        >
                          {flagLabel(f)}
                        </span>
                      ))}
                    </span>
                  </li>
                ))}
              </ul>
            )}
          </div>
        )}

        {/* Action buttons */}
        {canDecide ? (
          <div className="flex flex-wrap items-center gap-2 pt-1">
            <button
              type="button"
              onClick={() => submit("approve")}
              disabled={pending || cluster.approvable_count === 0}
              className={cn(
                "inline-flex h-10 flex-1 items-center justify-center gap-1.5 rounded-lg px-4 text-sm font-medium",
                "bg-credit/15 text-credit hover:bg-credit/25 disabled:opacity-50 sm:flex-initial sm:min-w-[200px]"
              )}
            >
              {pending && progress ? (
                <>
                  <Loader2 size={14} className="animate-spin" />
                  {progress.done}/{progress.total}
                </>
              ) : (
                <>
                  <Check size={14} />
                  Approve all {cluster.approvable_count} to{" "}
                  <span className="font-mono">{overrideCategory}</span>
                </>
              )}
            </button>
            <button
              type="button"
              onClick={() => submit("skip")}
              disabled={pending}
              className="inline-flex h-10 items-center gap-1.5 rounded-lg border border-subtle bg-elevated px-3 text-sm text-ink-secondary hover:bg-surface disabled:opacity-50"
            >
              <SkipForward size={13} />
              Skip
            </button>
            <button
              type="button"
              onClick={() => submit("reject")}
              disabled={pending}
              className="inline-flex h-10 items-center gap-1.5 rounded-lg border border-subtle bg-elevated px-3 text-sm text-debit hover:bg-debit/10 disabled:opacity-50"
              title="Reject all — ledger untouched"
            >
              <X size={13} />
              Reject all
            </button>
          </div>
        ) : (
          <p className="text-[11px] text-ink-tertiary">
            <Flag size={10} className="inline" /> Admin role required to decide.
          </p>
        )}

        {pending && progress && progress.total > 0 && (
          <div className="h-1 w-full overflow-hidden rounded-full bg-elevated">
            <div
              className="h-full bg-credit transition-all"
              style={{
                width: `${(progress.done / Math.max(progress.total, 1)) * 100}%`
              }}
            />
          </div>
        )}

        {error && (
          <p className="rounded-md border border-debit/40 bg-debit/10 px-3 py-2 text-[11px] text-debit">
            {error}
          </p>
        )}
      </div>
    </div>
  );
}
