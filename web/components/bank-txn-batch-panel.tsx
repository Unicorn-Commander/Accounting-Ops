"use client";

/**
 * BankTxnBatchPanel
 *
 * Full-page client component that owns:
 *   - the list of bank_transaction_import approval rows
 *   - filter bar (year, tier, category, suggestion presence)
 *   - per-row checkbox + select all / none / page
 *   - batch action bar (approve selected / reject selected)
 *   - confirmation modal before submitting
 *   - parallel fan-out with progress indicator (option b: no backend change needed)
 */

import { useRouter } from "next/navigation";
import { useState, useMemo, useTransition } from "react";
import {
  Check,
  X,
  Loader2,
  ChevronDown,
  AlertTriangle
} from "lucide-react";
import { cn } from "@/lib/cn";
import { BankTxnImportRow } from "@/components/bank-txn-import-row";
import type { Approval } from "@/lib/approvals";

// -----------------------------------------------------------------------
// Filter helpers
// -----------------------------------------------------------------------

function extractYear(sxid: string | undefined): string {
  // bofa-1792:YYYY-MM-DD:... pattern
  if (!sxid) return "Unknown";
  const m = sxid.match(/:(\d{4})-\d{2}-\d{2}/);
  return m ? m[1] : "Unknown";
}

// -----------------------------------------------------------------------
// Confirmation modal
// -----------------------------------------------------------------------

function ConfirmModal({
  action,
  count,
  samples,
  categoryOverride,
  onConfirm,
  onCancel,
  loading
}: {
  action: "approve" | "reject";
  count: number;
  samples: string[];
  categoryOverride: string;
  onConfirm: () => void;
  onCancel: () => void;
  loading: boolean;
}) {
  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/60 p-4">
      <div className="w-full max-w-md rounded-2xl border border-default bg-surface p-6 shadow-xl">
        <div className="flex items-start gap-3">
          <AlertTriangle
            size={20}
            className={action === "approve" ? "text-pending" : "text-debit"}
          />
          <div>
            <h2 className="text-base font-semibold">
              {action === "approve" ? "Approve" : "Reject"} {count} transaction
              {count !== 1 ? "s" : ""}?
            </h2>
            {action === "approve" && categoryOverride && (
              <p className="mt-1 text-xs text-ink-secondary">
                Category override:{" "}
                <span className="font-medium text-ink-primary">
                  {categoryOverride}
                </span>{" "}
                will be applied to all selected.
              </p>
            )}
            {action === "approve" && !categoryOverride && (
              <p className="mt-1 text-xs text-ink-secondary">
                Each transaction will post using its suggested category.
              </p>
            )}
            {action === "reject" && (
              <p className="mt-1 text-xs text-ink-secondary">
                The ledger will NOT be touched. Rows will be marked rejected.
              </p>
            )}
            {samples.length > 0 && (
              <div className="mt-3">
                <p className="mb-1 text-[10px] uppercase tracking-wider text-ink-tertiary">
                  Sample ({Math.min(samples.length, 5)} of {count})
                </p>
                <ul className="space-y-0.5">
                  {samples.slice(0, 5).map((s) => (
                    <li
                      key={s}
                      className="font-mono text-[11px] text-ink-secondary"
                    >
                      {s}
                    </li>
                  ))}
                </ul>
              </div>
            )}
          </div>
        </div>
        <div className="mt-5 flex justify-end gap-2">
          <button
            onClick={onCancel}
            disabled={loading}
            className="inline-flex h-8 items-center rounded-md border border-subtle bg-surface px-3 text-xs text-ink-secondary hover:bg-elevated disabled:opacity-50"
          >
            Cancel
          </button>
          <button
            onClick={onConfirm}
            disabled={loading}
            className={cn(
              "inline-flex h-8 items-center gap-1.5 rounded-md px-3 text-xs font-medium disabled:opacity-50",
              action === "approve"
                ? "bg-credit/15 text-credit hover:bg-credit/25"
                : "bg-debit/15 text-debit hover:bg-debit/25"
            )}
          >
            {loading && <Loader2 size={12} className="animate-spin" />}
            {action === "approve" ? "Approve all" : "Reject all"}
          </button>
        </div>
      </div>
    </div>
  );
}

// -----------------------------------------------------------------------
// Main panel
// -----------------------------------------------------------------------

const PAGE_SIZE = 50;

export function BankTxnBatchPanel({
  approvals,
  workspaceId,
  canDecide,
  categories
}: {
  approvals: Approval[];
  workspaceId: string;
  canDecide: boolean;
  categories: string[];
}) {
  const router = useRouter();

  // ---- filter state ----
  const [filterYear, setFilterYear] = useState<string>("all");
  const [filterTier, setFilterTier] = useState<string>("all");
  const [filterCategory, setFilterCategory] = useState<string>("all");
  const [filterSuggestion, setFilterSuggestion] = useState<string>("all");

  // ---- pagination ----
  const [page, setPage] = useState(0);

  // ---- selection ----
  const [selected, setSelected] = useState<Set<string>>(new Set());

  // ---- batch action ----
  const [batchCategoryOverride, setBatchCategoryOverride] = useState("");
  const [pendingAction, setPendingAction] = useState<"approve" | "reject" | null>(null);
  const [batchRunning, startBatchTransition] = useTransition();
  const [batchProgress, setBatchProgress] = useState<{
    done: number;
    total: number;
    errors: string[];
  } | null>(null);

  // ---- decided rows (removed from list after action) ----
  const [decidedIds, setDecidedIds] = useState<Set<string>>(new Set());

  // ---- derived lists ----
  const years = useMemo(() => {
    const set = new Set(
      approvals.map((a) => extractYear(a.payload?.source_external_id))
    );
    return ["all", ...Array.from(set).sort().reverse()];
  }, [approvals]);

  const categoryOptions = useMemo(() => {
    const set = new Set(
      approvals.map((a) => a.payload?.category ?? "Uncategorized")
    );
    return ["all", ...Array.from(set).sort()];
  }, [approvals]);

  const filteredApprovals = useMemo(() => {
    return approvals.filter((a) => {
      if (decidedIds.has(a.id)) return false;
      const p = a.payload ?? {};
      if (filterYear !== "all") {
        if (extractYear(p.source_external_id) !== filterYear) return false;
      }
      if (filterTier !== "all") {
        if ((p.materiality_tier ?? "") !== filterTier) return false;
      }
      if (filterCategory !== "all") {
        const cat = p.category ?? "Uncategorized";
        if (cat !== filterCategory) return false;
      }
      if (filterSuggestion !== "all") {
        const hasSuggestion = !!(
          p.category && p.categorization_source && p.category !== "Uncategorized"
        );
        if (filterSuggestion === "suggested" && !hasSuggestion) return false;
        if (filterSuggestion === "uncategorized" && hasSuggestion) return false;
      }
      return true;
    });
  }, [approvals, decidedIds, filterYear, filterTier, filterCategory, filterSuggestion]);

  const pageApprovals = filteredApprovals.slice(
    page * PAGE_SIZE,
    (page + 1) * PAGE_SIZE
  );
  const totalPages = Math.ceil(filteredApprovals.length / PAGE_SIZE);

  // ---- selection helpers ----
  function toggleOne(id: string) {
    setSelected((prev) => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  }

  function selectAll() {
    setSelected(new Set(filteredApprovals.map((a) => a.id)));
  }

  function selectNone() {
    setSelected(new Set());
  }

  function selectPage() {
    setSelected((prev) => {
      const next = new Set(prev);
      pageApprovals.forEach((a) => next.add(a.id));
      return next;
    });
  }

  const selectedOnPage = pageApprovals.filter((a) => selected.has(a.id)).length;

  // ---- batch submit ----
  function executeBatch() {
    if (!pendingAction) return;
    const ids = Array.from(selected).filter((id) => !decidedIds.has(id));
    const action = pendingAction;
    const catOverride = batchCategoryOverride;

    startBatchTransition(async () => {
      setBatchProgress({ done: 0, total: ids.length, errors: [] });

      const errors: string[] = [];
      const newlyDecided = new Set<string>();

      // Fan out in parallel batches of 10 to avoid thundering herd
      const CONCURRENCY = 10;
      for (let i = 0; i < ids.length; i += CONCURRENCY) {
        const chunk = ids.slice(i, i + CONCURRENCY);
        await Promise.all(
          chunk.map(async (id) => {
            try {
              const body: Record<string, unknown> = { decision: action };
              if (action === "approve" && catOverride) {
                body.category_override = catOverride;
              }
              const res = await fetch(
                `/api/workspaces/${workspaceId}/approvals/${id}/decide`,
                {
                  method: "POST",
                  headers: { "Content-Type": "application/json" },
                  body: JSON.stringify(body)
                }
              );
              if (!res.ok) {
                const j = await res.json().catch(() => ({})) as { detail?: string };
                errors.push(`${id}: ${j.detail ?? res.status}`);
              } else {
                newlyDecided.add(id);
              }
            } catch (e) {
              errors.push(`${id}: ${e instanceof Error ? e.message : "error"}`);
            }
          })
        );
        setBatchProgress((prev) =>
          prev
            ? {
                done: prev.done + chunk.length,
                total: prev.total,
                errors: [...prev.errors, ...errors.slice(prev.errors.length)]
              }
            : null
        );
      }

      // Remove decided rows from selection + the list
      setDecidedIds((prev) => new Set([...prev, ...newlyDecided]));
      setSelected((prev) => {
        const next = new Set(prev);
        newlyDecided.forEach((id) => next.delete(id));
        return next;
      });
      setPendingAction(null);
      setBatchProgress(null);
      router.refresh();
    });
  }

  const selCount = selected.size;

  return (
    <div className="flex flex-col gap-4">
      {/* ---- Filter bar ---- */}
      <div className="flex flex-wrap items-center gap-2 rounded-xl border border-default bg-elevated px-4 py-2.5">
        <span className="text-[11px] uppercase tracking-wider text-ink-tertiary">
          Filters
        </span>

        <FilterSelect
          label="Year"
          value={filterYear}
          onChange={(v) => { setFilterYear(v); setPage(0); }}
          options={years.map((y) => ({ value: y, label: y === "all" ? "All years" : y }))}
        />
        <FilterSelect
          label="Tier"
          value={filterTier}
          onChange={(v) => { setFilterTier(v); setPage(0); }}
          options={[
            { value: "all", label: "All tiers" },
            { value: "L", label: "L — Low" },
            { value: "M", label: "M — Medium" },
            { value: "H", label: "H — High" }
          ]}
        />
        <FilterSelect
          label="Category"
          value={filterCategory}
          onChange={(v) => { setFilterCategory(v); setPage(0); }}
          options={categoryOptions.map((c) => ({
            value: c,
            label: c === "all" ? "All categories" : c
          }))}
        />
        <FilterSelect
          label="Suggestion"
          value={filterSuggestion}
          onChange={(v) => { setFilterSuggestion(v); setPage(0); }}
          options={[
            { value: "all", label: "All" },
            { value: "suggested", label: "Has suggestion" },
            { value: "uncategorized", label: "Uncategorized" }
          ]}
        />

        <span className="ml-auto text-xs text-ink-tertiary">
          {filteredApprovals.length} matching
        </span>
      </div>

      {/* ---- Select controls ---- */}
      {canDecide && filteredApprovals.length > 0 && (
        <div className="flex flex-wrap items-center gap-2">
          <button
            onClick={selectAll}
            className="text-xs text-brand-400 hover:underline"
          >
            Select all ({filteredApprovals.length})
          </button>
          <span className="text-ink-tertiary">·</span>
          <button
            onClick={selectPage}
            className="text-xs text-brand-400 hover:underline"
          >
            Select page ({pageApprovals.length})
          </button>
          <span className="text-ink-tertiary">·</span>
          <button
            onClick={selectNone}
            className="text-xs text-ink-tertiary hover:underline"
          >
            Select none
          </button>
          {selCount > 0 && (
            <span className="ml-2 text-xs font-medium text-ink-primary">
              {selCount} selected
            </span>
          )}
        </div>
      )}

      {/* ---- Batch action bar ---- */}
      {canDecide && selCount > 0 && (
        <div className="flex flex-wrap items-center gap-2 rounded-xl border border-brand-500/40 bg-brand-500/[0.06] px-4 py-2.5">
          <span className="text-xs font-medium text-ink-primary">
            {selCount} selected
          </span>

          {/* Optional batch category override */}
          {categories.length > 0 && (
            <select
              value={batchCategoryOverride}
              onChange={(e) => setBatchCategoryOverride(e.target.value)}
              className="h-7 rounded border border-subtle bg-surface px-2 text-xs text-ink-primary focus:outline-none focus:ring-1 focus:ring-brand-500"
              aria-label="Batch category override"
            >
              <option value="">Keep each suggested category</option>
              {categories.map((c) => (
                <option key={c} value={c}>
                  {c}
                </option>
              ))}
            </select>
          )}

          <button
            onClick={() => setPendingAction("approve")}
            disabled={batchRunning}
            className="inline-flex h-7 items-center gap-1 rounded-md bg-credit/15 px-3 text-xs font-medium text-credit hover:bg-credit/25 disabled:opacity-50"
          >
            <Check size={12} /> Approve selected
          </button>
          <button
            onClick={() => setPendingAction("reject")}
            disabled={batchRunning}
            className="inline-flex h-7 items-center gap-1 rounded-md bg-debit/10 px-3 text-xs font-medium text-debit hover:bg-debit/20 disabled:opacity-50"
          >
            <X size={12} /> Reject selected
          </button>

          {batchRunning && batchProgress && (
            <span className="ml-auto flex items-center gap-1 text-xs text-ink-secondary">
              <Loader2 size={12} className="animate-spin" />
              {batchProgress.done} / {batchProgress.total}
              {batchProgress.errors.length > 0 && (
                <span className="text-debit">
                  {" "}
                  ({batchProgress.errors.length} errors)
                </span>
              )}
            </span>
          )}
        </div>
      )}

      {/* ---- Row list ---- */}
      {filteredApprovals.length === 0 ? (
        <div className="rounded-xl border border-default bg-surface px-5 py-8 text-center">
          <p className="text-sm text-ink-secondary">
            Nothing to review — books are caught up.
          </p>
        </div>
      ) : (
        <div className="flex flex-col gap-2">
          {pageApprovals.map((a) => (
            <BankTxnImportRow
              key={a.id}
              approval={a}
              workspaceId={workspaceId}
              canDecide={canDecide}
              categories={categories}
              batchMode={canDecide}
              selected={selected.has(a.id)}
              onToggleSelect={toggleOne}
              onDecided={(id) => {
                setDecidedIds((prev) => new Set([...prev, id]));
                setSelected((prev) => {
                  const next = new Set(prev);
                  next.delete(id);
                  return next;
                });
              }}
            />
          ))}
        </div>
      )}

      {/* ---- Pagination ---- */}
      {totalPages > 1 && (
        <div className="flex items-center justify-center gap-3 pt-2">
          <button
            disabled={page === 0}
            onClick={() => setPage((p) => p - 1)}
            className="inline-flex h-7 items-center rounded border border-subtle bg-surface px-3 text-xs text-ink-secondary hover:bg-elevated disabled:opacity-40"
          >
            Previous
          </button>
          <span className="text-xs text-ink-tertiary">
            Page {page + 1} of {totalPages}
          </span>
          <button
            disabled={page >= totalPages - 1}
            onClick={() => setPage((p) => p + 1)}
            className="inline-flex h-7 items-center rounded border border-subtle bg-surface px-3 text-xs text-ink-secondary hover:bg-elevated disabled:opacity-40"
          >
            Next
          </button>
        </div>
      )}

      {/* ---- Confirmation modal ---- */}
      {pendingAction && (
        <ConfirmModal
          action={pendingAction}
          count={selCount}
          samples={Array.from(selected)
            .slice(0, 5)
            .map((id) => {
              const a = approvals.find((x) => x.id === id);
              return (
                a?.payload?.source_external_id ??
                a?.payload?.description ??
                id
              );
            })}
          categoryOverride={batchCategoryOverride}
          onConfirm={executeBatch}
          onCancel={() => setPendingAction(null)}
          loading={batchRunning}
        />
      )}
    </div>
  );
}

// -----------------------------------------------------------------------
// Small filter select component
// -----------------------------------------------------------------------

function FilterSelect({
  label,
  value,
  onChange,
  options
}: {
  label: string;
  value: string;
  onChange: (v: string) => void;
  options: { value: string; label: string }[];
}) {
  return (
    <label className="flex items-center gap-1">
      <span className="sr-only">{label}</span>
      <div className="relative flex items-center">
        <select
          value={value}
          onChange={(e) => onChange(e.target.value)}
          className="h-7 appearance-none rounded border border-subtle bg-surface py-0 pl-2 pr-6 text-xs text-ink-primary focus:outline-none focus:ring-1 focus:ring-brand-500"
        >
          {options.map((o) => (
            <option key={o.value} value={o.value}>
              {o.label}
            </option>
          ))}
        </select>
        <ChevronDown
          size={11}
          className="pointer-events-none absolute right-1.5 text-ink-tertiary"
        />
      </div>
    </label>
  );
}
