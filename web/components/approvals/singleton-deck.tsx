"use client";

/**
 * SingletonDeck
 *
 * One-card-at-a-time swipe flow for the singletons (clusters that ended up
 * size 1 after Books Lead Phase 3 vendor-grouping). Optimized for fast
 * keyboard-driven review on desktop AND tap-driven review on iPhone.
 *
 * Controls:
 *   ✓ Approve         (A or Enter)
 *   ⤳ Skip            (S)
 *   ✗ Reject          (R)
 *   < Back (undo nav) (←)
 *   > Next            (→)
 *
 * Skip is purely local — the singleton is moved back into a queue at the
 * end. Approve/Reject hit the same single-row decide endpoint the existing
 * /approvals page uses (so post_journal_entry, audit, hash chain all behave
 * identically).
 *
 * Sticky bottom action bar on mobile (≤ sm). Large 44pt+ tap targets.
 */

import { useState, useEffect, useCallback, useRef, useMemo } from "react";
import { useRouter } from "next/navigation";
import {
  Check,
  X,
  SkipForward,
  ChevronLeft,
  ChevronRight,
  Loader2,
  AlertTriangle,
  Sparkles
} from "lucide-react";
import { cn } from "@/lib/cn";
import {
  type ApprovalItem,
  type ClusterConfidence,
  formatUsdPrecise,
  confidenceLabel,
  flagLabel
} from "@/lib/approval-cluster-format";

// -----------------------------------------------------------------------
// Confidence accent
// -----------------------------------------------------------------------

function confidenceAccent(c: ClusterConfidence): string {
  return c === "high"
    ? "border-credit/40"
    : c === "medium"
      ? "border-pending/40"
      : "border-debit/40";
}

function ConfidencePill({ c }: { c: ClusterConfidence }) {
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
      <Sparkles size={9} />
      {confidenceLabel(c)}
    </span>
  );
}

// -----------------------------------------------------------------------
// Main deck
// -----------------------------------------------------------------------

export function SingletonDeck({
  items,
  categories,
  workspaceId,
  canDecide,
  onDecidedOne
}: {
  items: ApprovalItem[];
  categories: string[];
  workspaceId: string;
  canDecide: boolean;
  /** Notifies parent (for the progress + final summary screen) */
  onDecidedOne: (
    item: ApprovalItem,
    decision: "approve" | "reject" | "skip"
  ) => void;
}) {
  const router = useRouter();
  const [cursor, setCursor] = useState(0);
  const [decisions, setDecisions] = useState<
    Record<string, "approve" | "reject" | "skip">
  >({});
  const [overrides, setOverrides] = useState<Record<string, string>>({});
  const [pendingId, setPendingId] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  // Filter out items that have already been decided in this session — they
  // shouldn't show up again on Back nav unless explicitly undone.
  const liveItems = useMemo(
    () => items.filter((it) => !decisions[it.approval_id]),
    [items, decisions]
  );

  const current: ApprovalItem | undefined = liveItems[cursor];
  const totalDecided = Object.keys(decisions).length;
  const totalAll = items.length;

  // Reset cursor if it walks past the end (e.g. after skip-loops).
  useEffect(() => {
    if (cursor >= liveItems.length) {
      setCursor(Math.max(liveItems.length - 1, 0));
    }
  }, [cursor, liveItems.length]);

  // Initialize override category for newly visible item.
  useEffect(() => {
    if (!current) return;
    if (!(current.approval_id in overrides) && current.category_suggested) {
      setOverrides((prev) => ({
        ...prev,
        [current.approval_id]: current.category_suggested ?? ""
      }));
    }
  }, [current, overrides]);

  const goNext = useCallback(() => {
    setCursor((c) => Math.min(c + 1, Math.max(liveItems.length - 1, 0)));
  }, [liveItems.length]);

  const goBack = useCallback(() => {
    setCursor((c) => Math.max(c - 1, 0));
  }, []);

  const submitDecision = useCallback(
    async (decision: "approve" | "reject" | "skip") => {
      if (!current) return;
      if (!canDecide && decision !== "skip") return;
      if (pendingId === current.approval_id) return;

      const item = current;
      setError(null);

      setPendingId(item.approval_id);
      try {
        let endpoint: string;
        let body: Record<string, unknown>;
        if (decision === "skip") {
          // Durable skip — server-side flip to status='skipped' + audit row.
          endpoint = `/api/workspaces/${workspaceId}/approvals/${item.approval_id}/skip`;
          body = {};
        } else {
          endpoint = `/api/workspaces/${workspaceId}/approvals/${item.approval_id}/decide`;
          body = { decision };
          if (decision === "approve") {
            const cat = overrides[item.approval_id];
            if (cat && cat !== item.category_suggested) {
              body.category_override = cat;
            }
          }
        }
        const res = await fetch(endpoint, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(body),
        });
        const j = (await res.json().catch(() => ({}))) as { detail?: string };
        if (!res.ok) throw new Error(j.detail ?? `HTTP ${res.status}`);

        setDecisions((prev) => ({ ...prev, [item.approval_id]: decision }));
        onDecidedOne(item, decision);
        // Mild refresh — server totals update on next page render.
        router.refresh();
      } catch (e) {
        setError(e instanceof Error ? e.message : "Unknown error");
      } finally {
        setPendingId(null);
      }
    },
    [current, canDecide, pendingId, overrides, workspaceId, router, onDecidedOne]
  );

  // -----------------------------------------------------------------------
  // Keyboard shortcuts
  // -----------------------------------------------------------------------

  const containerRef = useRef<HTMLDivElement | null>(null);
  useEffect(() => {
    function onKey(e: KeyboardEvent) {
      // Ignore when typing into form controls.
      const t = e.target as HTMLElement;
      if (
        t &&
        (t.tagName === "INPUT" ||
          t.tagName === "TEXTAREA" ||
          t.tagName === "SELECT")
      ) {
        return;
      }
      if (e.key === "a" || e.key === "A" || e.key === "Enter") {
        e.preventDefault();
        submitDecision("approve");
      } else if (e.key === "s" || e.key === "S") {
        e.preventDefault();
        submitDecision("skip");
      } else if (e.key === "r" || e.key === "R") {
        e.preventDefault();
        submitDecision("reject");
      } else if (e.key === "ArrowRight") {
        e.preventDefault();
        goNext();
      } else if (e.key === "ArrowLeft") {
        e.preventDefault();
        goBack();
      }
    }
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [submitDecision, goNext, goBack]);

  // -----------------------------------------------------------------------
  // Render
  // -----------------------------------------------------------------------

  if (liveItems.length === 0 || !current) {
    return (
      <div className="rounded-2xl border border-default bg-surface px-6 py-12 text-center">
        <Check size={28} className="mx-auto mb-3 text-credit" />
        <p className="text-base font-medium text-ink-primary">
          All singletons handled
        </p>
        <p className="mt-1 text-sm text-ink-tertiary">
          {totalDecided} decisions made
          {totalAll - totalDecided > 0
            ? ` (${totalAll - totalDecided} skipped — pull back into the deck above)`
            : ""}
        </p>
      </div>
    );
  }

  const flags = current.flags ?? [];
  const isFlagged = flags.length > 0;
  const overrideCat = overrides[current.approval_id] ?? current.category_suggested ?? "";
  const categoryOptions = (() => {
    const opts = new Set<string>(categories);
    if (current.category_suggested) opts.add(current.category_suggested);
    return Array.from(opts).sort();
  })();

  // Progress is "decisions made out of all singletons" (incl. skipped).
  const progressPct = totalAll > 0 ? (totalDecided / totalAll) * 100 : 0;

  return (
    <div ref={containerRef} className="flex flex-col gap-3" tabIndex={-1}>
      {/* Progress strip + nav */}
      <div className="flex items-center gap-3 text-xs text-ink-secondary">
        <div className="flex items-center gap-1.5">
          <button
            type="button"
            onClick={goBack}
            disabled={cursor === 0}
            className="inline-flex h-7 w-7 items-center justify-center rounded-md border border-subtle bg-elevated text-ink-secondary hover:bg-surface disabled:opacity-30"
            aria-label="Previous (←)"
            title="Previous (←)"
          >
            <ChevronLeft size={14} />
          </button>
          <button
            type="button"
            onClick={goNext}
            disabled={cursor >= liveItems.length - 1}
            className="inline-flex h-7 w-7 items-center justify-center rounded-md border border-subtle bg-elevated text-ink-secondary hover:bg-surface disabled:opacity-30"
            aria-label="Next (→)"
            title="Next (→)"
          >
            <ChevronRight size={14} />
          </button>
        </div>
        <span className="font-mono tabular-nums">
          {cursor + 1} / {liveItems.length}
        </span>
        <span className="text-ink-tertiary">·</span>
        <span>
          {totalDecided} of {totalAll} decided
        </span>
        <div className="ml-auto hidden text-ink-tertiary sm:flex sm:items-center sm:gap-2">
          <kbd className="rounded border border-subtle bg-elevated px-1.5 py-0.5 font-mono text-[10px]">
            A
          </kbd>
          approve
          <kbd className="rounded border border-subtle bg-elevated px-1.5 py-0.5 font-mono text-[10px]">
            S
          </kbd>
          skip
          <kbd className="rounded border border-subtle bg-elevated px-1.5 py-0.5 font-mono text-[10px]">
            R
          </kbd>
          reject
        </div>
      </div>

      {/* Progress bar */}
      <div className="h-1 w-full overflow-hidden rounded-full bg-elevated">
        <div
          className="h-full bg-brand-500 transition-all"
          style={{ width: `${progressPct}%` }}
        />
      </div>

      {/* The card */}
      <div
        className={cn(
          "rounded-2xl border-2 bg-surface p-5 sm:p-6 transition-colors",
          confidenceAccent(current.confidence)
        )}
      >
        {/* Header */}
        <div className="flex flex-wrap items-start gap-x-3 gap-y-1.5">
          <ConfidencePill c={current.confidence} />
          {isFlagged && (
            <span className="inline-flex items-center gap-1 rounded-full bg-pending/20 px-2 py-0.5 text-[10px] font-semibold uppercase tracking-wider text-pending">
              <AlertTriangle size={9} />
              {flags.map(flagLabel).join(" · ")}
            </span>
          )}
          {current.materiality_tier && (
            <span className="inline-flex items-center rounded-full bg-elevated px-2 py-0.5 text-[10px] font-semibold uppercase tracking-wider text-ink-tertiary">
              Tier {current.materiality_tier}
            </span>
          )}
        </div>

        {/* Vendor + amount */}
        <div className="mt-4 flex flex-col gap-3 sm:flex-row sm:items-start sm:justify-between">
          <div className="min-w-0 flex-1">
            <p className="text-[10px] uppercase tracking-wider text-ink-tertiary">
              Vendor
            </p>
            <h2 className="mt-1 break-words text-xl font-semibold text-ink-primary sm:text-2xl">
              {current.vendor || "—"}
            </h2>
            {current.payee && current.payee !== current.vendor && (
              <p className="mt-0.5 text-xs text-ink-tertiary">
                Payee: <span className="font-mono">{current.payee}</span>
              </p>
            )}
          </div>
          <div className="shrink-0 text-left sm:text-right">
            <p className="text-[10px] uppercase tracking-wider text-ink-tertiary">
              Amount
            </p>
            <p className="mt-1 font-mono text-3xl font-semibold text-ink-primary">
              {formatUsdPrecise(current.amount_usd)}
            </p>
            <p className="mt-0.5 text-xs text-ink-tertiary">
              {current.date ?? "—"}
            </p>
          </div>
        </div>

        {/* Memo */}
        {current.memo && (
          <div className="mt-3 rounded-lg border border-default bg-elevated px-3 py-2">
            <p className="text-[10px] uppercase tracking-wider text-ink-tertiary">
              Memo
            </p>
            <p className="mt-1 text-sm text-ink-secondary">{current.memo}</p>
          </div>
        )}

        {/* Rationale */}
        {current.rationale && (
          <div className="mt-3">
            <p className="text-[10px] uppercase tracking-wider text-ink-tertiary">
              Why this was flagged
            </p>
            <p className="mt-1 text-xs leading-relaxed text-ink-secondary">
              {current.rationale}
            </p>
          </div>
        )}

        {/* Category override */}
        <div className="mt-4">
          <label
            className="text-[10px] uppercase tracking-wider text-ink-tertiary"
            htmlFor={`single-cat-${current.approval_id}`}
          >
            Post to category
          </label>
          <select
            id={`single-cat-${current.approval_id}`}
            value={overrideCat}
            onChange={(e) =>
              setOverrides((prev) => ({
                ...prev,
                [current.approval_id]: e.target.value
              }))
            }
            disabled={pendingId === current.approval_id || !canDecide}
            className="mt-1 h-10 w-full rounded-lg border border-subtle bg-elevated px-3 text-sm text-ink-primary focus:outline-none focus:ring-1 focus:ring-brand-500 disabled:opacity-50"
          >
            {categoryOptions.length === 0 && (
              <option value="">{current.category_suggested ?? "—"}</option>
            )}
            {categoryOptions.map((c) => (
              <option key={c} value={c}>
                {c}
              </option>
            ))}
          </select>
        </div>

        {/* Errors */}
        {error && (
          <p className="mt-3 rounded-md border border-debit/40 bg-debit/10 px-3 py-2 text-xs text-debit">
            {error}
          </p>
        )}

        {/* Desktop actions (sticky-bottom on mobile, inline here) */}
        <div className="mt-5 hidden flex-wrap items-center gap-2 sm:flex">
          <button
            type="button"
            onClick={() => submitDecision("approve")}
            disabled={pendingId === current.approval_id || !canDecide}
            className="inline-flex h-11 flex-1 items-center justify-center gap-2 rounded-lg bg-credit/15 px-4 text-sm font-medium text-credit hover:bg-credit/25 disabled:opacity-50 sm:flex-initial sm:min-w-[140px]"
          >
            {pendingId === current.approval_id ? (
              <Loader2 size={14} className="animate-spin" />
            ) : (
              <Check size={14} />
            )}
            Approve
          </button>
          <button
            type="button"
            onClick={() => submitDecision("skip")}
            disabled={pendingId === current.approval_id}
            className="inline-flex h-11 items-center gap-2 rounded-lg border border-subtle bg-elevated px-4 text-sm text-ink-secondary hover:bg-surface disabled:opacity-50"
          >
            <SkipForward size={13} />
            Skip
          </button>
          <button
            type="button"
            onClick={() => submitDecision("reject")}
            disabled={pendingId === current.approval_id || !canDecide}
            className="inline-flex h-11 items-center gap-2 rounded-lg border border-subtle bg-elevated px-4 text-sm text-debit hover:bg-debit/10 disabled:opacity-50"
          >
            <X size={13} />
            Reject
          </button>
        </div>
      </div>

      {/* Mobile sticky action bar */}
      <div
        className={cn(
          "sticky bottom-0 -mx-4 mt-1 border-t border-default bg-surface/95 px-4 py-3 backdrop-blur",
          "flex items-center gap-2 sm:hidden"
        )}
      >
        <button
          type="button"
          onClick={() => submitDecision("reject")}
          disabled={pendingId === current.approval_id || !canDecide}
          aria-label="Reject"
          className="flex h-12 w-12 items-center justify-center rounded-full bg-debit/15 text-debit active:bg-debit/30 disabled:opacity-50"
        >
          <X size={20} />
        </button>
        <button
          type="button"
          onClick={() => submitDecision("skip")}
          disabled={pendingId === current.approval_id}
          aria-label="Skip"
          className="flex h-12 w-12 items-center justify-center rounded-full border border-subtle bg-elevated text-ink-secondary active:bg-surface disabled:opacity-50"
        >
          <SkipForward size={18} />
        </button>
        <button
          type="button"
          onClick={() => submitDecision("approve")}
          disabled={pendingId === current.approval_id || !canDecide}
          className="flex h-12 flex-1 items-center justify-center gap-2 rounded-full bg-credit/15 text-sm font-semibold text-credit active:bg-credit/30 disabled:opacity-50"
        >
          {pendingId === current.approval_id ? (
            <Loader2 size={18} className="animate-spin" />
          ) : (
            <Check size={18} />
          )}
          Approve
        </button>
      </div>
    </div>
  );
}
