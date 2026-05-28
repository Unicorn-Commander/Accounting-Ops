"use client";

/**
 * BankTxnGroupedView
 *
 * "Group by category" mode for /approvals. Collapses the (potentially
 * hundreds of) bank_transaction_import rows by their suggested category so
 * the reviewer can sanity-check + bulk-approve a whole group at once —
 * "131 SaaS·Cloud rows totaling $4,238 — approve all".
 *
 * Workflow:
 *   - Each group card shows: category name, row count, summed amount,
 *     3-row sample, CPA-flag warning if applicable.
 *   - "Approve all in group" fans out the same per-row decide POST in
 *     parallel batches of 10 (same code path as the row-mode batch panel,
 *     so per-row materiality refusals + audit + hash-chain advance all
 *     happen correctly — no new ledger paths added).
 *   - The "Uncategorized" group gets a category-picker dropdown so the
 *     reviewer applies the right category as a category_override for
 *     every row in the group, then approves.
 *
 * Categorization is data, not policy: this component never decides what
 * the right category IS — it just makes it cheap to apply the reviewer's
 * choice across many rows.
 */

import { useState, useMemo, useTransition } from "react";
import { useRouter } from "next/navigation";
import {
  Check,
  ChevronDown,
  ChevronRight,
  Loader2,
  AlertTriangle,
  Sparkles,
  HelpCircle
} from "lucide-react";
import { cn } from "@/lib/cn";
import { BankTxnImportRow } from "@/components/bank-txn-import-row";
import type { Approval } from "@/lib/approvals";

// -----------------------------------------------------------------------
// CPA-flagged categories — same source of truth as web/lib/taxes-1120.ts.
// Keep in sync.
// -----------------------------------------------------------------------
const CPA_FLAGGED: Record<string, string> = {
  "Banking · Interest":
    "§163(j) interest limitation may apply; CPA must classify.",
  "SaaS · AI": "May qualify as COGS rather than deduction; CPA must split.",
  "Inter-entity transfer":
    "Management fees vs intercompany loan vs elimination — CPA classifies.",
  "Contractor · 1099-NEC":
    "If payee is an officer, belongs on Form 1125-E not Other deductions."
};

const UNCATEGORIZED_KEY = "Uncategorized";

// -----------------------------------------------------------------------
// Helpers
// -----------------------------------------------------------------------

function centsToDollars(c: number): string {
  const sign = c < 0 ? "-" : "";
  const abs = Math.abs(c);
  const dollars = Math.floor(abs / 100);
  const cents = abs % 100;
  return (
    sign +
    "$" +
    dollars.toLocaleString("en-US") +
    "." +
    cents.toString().padStart(2, "0")
  );
}

type GroupEntry = {
  category: string; // canonical category or UNCATEGORIZED_KEY
  approvals: Approval[];
  totalCents: number; // sum of payload.amount_cents
  inflowCents: number; // sum of positives
  outflowCents: number; // sum of negatives
  cpaFlag: string | null;
};

function groupByCategory(rows: Approval[]): GroupEntry[] {
  const byCat = new Map<string, Approval[]>();
  for (const a of rows) {
    const cat = a.payload?.category ?? UNCATEGORIZED_KEY;
    const list = byCat.get(cat);
    if (list) list.push(a);
    else byCat.set(cat, [a]);
  }
  const out: GroupEntry[] = [];
  for (const [cat, list] of byCat.entries()) {
    let total = 0;
    let inflow = 0;
    let outflow = 0;
    for (const a of list) {
      const amt = Number(a.payload?.amount_cents ?? 0);
      total += amt;
      if (amt > 0) inflow += amt;
      else outflow += amt;
    }
    out.push({
      category: cat,
      approvals: list,
      totalCents: total,
      inflowCents: inflow,
      outflowCents: outflow,
      cpaFlag: CPA_FLAGGED[cat] ?? null
    });
  }
  // Sort: Uncategorized last; then by count desc.
  out.sort((a, b) => {
    if (a.category === UNCATEGORIZED_KEY && b.category !== UNCATEGORIZED_KEY)
      return 1;
    if (b.category === UNCATEGORIZED_KEY && a.category !== UNCATEGORIZED_KEY)
      return -1;
    return b.approvals.length - a.approvals.length;
  });
  return out;
}

// -----------------------------------------------------------------------
// Group card
// -----------------------------------------------------------------------

function GroupCard({
  group,
  workspaceId,
  canDecide,
  categories,
  onDecided
}: {
  group: GroupEntry;
  workspaceId: string;
  canDecide: boolean;
  categories: string[];
  onDecided: (id: string) => void;
}) {
  const router = useRouter();
  const [expanded, setExpanded] = useState(false);
  const [overrideCategory, setOverrideCategory] = useState(
    group.category === UNCATEGORIZED_KEY ? "" : ""
  );
  const [confirmOpen, setConfirmOpen] = useState(false);
  const [running, startTransition] = useTransition();
  const [progress, setProgress] = useState<{
    done: number;
    total: number;
    errors: string[];
  } | null>(null);
  const [decidedInGroup, setDecidedInGroup] = useState<Set<string>>(new Set());

  const remaining = group.approvals.filter((a) => !decidedInGroup.has(a.id));
  const remainingCount = remaining.length;

  const isUncategorized = group.category === UNCATEGORIZED_KEY;
  // Uncategorized groups REQUIRE a category override before approve-all.
  const canApproveAll =
    canDecide &&
    remainingCount > 0 &&
    (!isUncategorized || overrideCategory.length > 0);

  function approveAll() {
    const ids = remaining.map((a) => a.id);
    const catOverride = overrideCategory || ""; // empty = keep suggested

    startTransition(async () => {
      setProgress({ done: 0, total: ids.length, errors: [] });

      const newlyDecided = new Set<string>();
      const errors: string[] = [];

      const CONCURRENCY = 10;
      for (let i = 0; i < ids.length; i += CONCURRENCY) {
        const chunk = ids.slice(i, i + CONCURRENCY);
        await Promise.all(
          chunk.map(async (id) => {
            try {
              const body: Record<string, unknown> = { decision: "approve" };
              if (catOverride) body.category_override = catOverride;
              const res = await fetch(
                `/api/workspaces/${workspaceId}/approvals/${id}/decide`,
                {
                  method: "POST",
                  headers: { "Content-Type": "application/json" },
                  body: JSON.stringify(body)
                }
              );
              if (!res.ok) {
                const j = (await res.json().catch(() => ({}))) as {
                  detail?: string;
                };
                errors.push(`${id}: ${j.detail ?? res.status}`);
              } else {
                newlyDecided.add(id);
              }
            } catch (e) {
              errors.push(
                `${id}: ${e instanceof Error ? e.message : "error"}`
              );
            }
          })
        );
        setProgress((prev) =>
          prev
            ? {
                done: prev.done + chunk.length,
                total: prev.total,
                errors
              }
            : null
        );
      }

      setDecidedInGroup((prev) => new Set([...prev, ...newlyDecided]));
      newlyDecided.forEach((id) => onDecided(id));
      setProgress(null);
      setConfirmOpen(false);
      router.refresh();
    });
  }

  return (
    <div
      className={cn(
        "rounded-xl border bg-surface",
        group.cpaFlag
          ? "border-amber-500/40"
          : isUncategorized
            ? "border-amber-500/30 bg-amber-500/[0.03]"
            : "border-default"
      )}
    >
      {/* Header */}
      <div className="flex flex-wrap items-center gap-3 px-4 py-3">
        <button
          onClick={() => setExpanded((e) => !e)}
          className="flex items-center gap-1.5 text-sm font-medium text-ink-primary"
          aria-label={expanded ? "Collapse" : "Expand"}
        >
          {expanded ? (
            <ChevronDown size={16} className="text-ink-secondary" />
          ) : (
            <ChevronRight size={16} className="text-ink-secondary" />
          )}
          <span>{group.category}</span>
        </button>

        {group.cpaFlag && (
          <span
            className="inline-flex items-center gap-1 rounded-full bg-amber-500/15 px-2 py-0.5 text-[10px] font-medium text-amber-300"
            title={group.cpaFlag}
          >
            <AlertTriangle size={10} />
            CPA review
          </span>
        )}

        {isUncategorized && (
          <span className="inline-flex items-center gap-1 rounded-full bg-amber-500/15 px-2 py-0.5 text-[10px] font-medium text-amber-300">
            <HelpCircle size={10} />
            Pick a category to approve
          </span>
        )}

        <span className="text-xs text-ink-secondary">
          {remainingCount} row{remainingCount !== 1 ? "s" : ""}
        </span>

        <span className="text-xs text-ink-tertiary">
          inflow {centsToDollars(group.inflowCents)} · outflow{" "}
          {centsToDollars(group.outflowCents)}
        </span>

        <div className="ml-auto flex items-center gap-2">
          {isUncategorized && (
            <select
              value={overrideCategory}
              onChange={(e) => setOverrideCategory(e.target.value)}
              className="h-7 rounded border border-subtle bg-surface px-2 text-xs text-ink-primary focus:outline-none focus:ring-1 focus:ring-brand-500"
              aria-label="Apply category to all in this group"
            >
              <option value="">Choose a category…</option>
              {categories.map((c) => (
                <option key={c} value={c}>
                  {c}
                </option>
              ))}
            </select>
          )}

          {!isUncategorized && canDecide && categories.length > 0 && (
            <select
              value={overrideCategory}
              onChange={(e) => setOverrideCategory(e.target.value)}
              className="h-7 rounded border border-subtle bg-surface px-2 text-xs text-ink-secondary focus:outline-none focus:ring-1 focus:ring-brand-500"
              aria-label="Override category for all in this group"
              title="Optionally override the suggested category before approving"
            >
              <option value="">Keep suggested ({group.category})</option>
              {categories.map((c) => (
                <option key={c} value={c}>
                  Override → {c}
                </option>
              ))}
            </select>
          )}

          {canDecide && (
            <button
              onClick={() => setConfirmOpen(true)}
              disabled={!canApproveAll || running}
              className={cn(
                "inline-flex h-7 items-center gap-1 rounded-md px-3 text-xs font-medium disabled:opacity-50",
                group.cpaFlag
                  ? "bg-amber-500/15 text-amber-300 hover:bg-amber-500/25"
                  : "bg-credit/15 text-credit hover:bg-credit/25"
              )}
              title={
                isUncategorized && !overrideCategory
                  ? "Pick a category first"
                  : "Approve every row in this group"
              }
            >
              {running ? (
                <Loader2 size={12} className="animate-spin" />
              ) : group.cpaFlag ? (
                <Sparkles size={12} />
              ) : (
                <Check size={12} />
              )}
              Approve all
            </button>
          )}
        </div>
      </div>

      {/* Progress (during running) */}
      {running && progress && (
        <div className="border-t border-subtle bg-elevated px-4 py-2 text-xs text-ink-secondary">
          <span className="inline-flex items-center gap-2">
            <Loader2 size={12} className="animate-spin" />
            {progress.done} / {progress.total} processed
            {progress.errors.length > 0 && (
              <span className="text-debit">
                · {progress.errors.length} errors
              </span>
            )}
          </span>
        </div>
      )}

      {/* Sample (collapsed) */}
      {!expanded && remainingCount > 0 && (
        <div className="border-t border-subtle px-4 pb-3 pt-2">
          <p className="mb-1 text-[10px] uppercase tracking-wider text-ink-tertiary">
            Sample ({Math.min(3, remainingCount)} of {remainingCount})
          </p>
          <ul className="space-y-0.5">
            {remaining.slice(0, 3).map((a) => (
              <li
                key={a.id}
                className="flex items-center gap-2 text-[11px] text-ink-secondary"
              >
                <span className="font-mono text-ink-tertiary">
                  {a.payload?.effective_date ?? "?"}
                </span>
                <span className="truncate">
                  {a.payload?.description ?? "(no description)"}
                </span>
                <span
                  className={cn(
                    "ml-auto font-mono shrink-0",
                    Number(a.payload?.amount_cents ?? 0) < 0
                      ? "text-debit"
                      : "text-credit"
                  )}
                >
                  {centsToDollars(Number(a.payload?.amount_cents ?? 0))}
                </span>
              </li>
            ))}
          </ul>
        </div>
      )}

      {/* Full row list (expanded) */}
      {expanded && remainingCount > 0 && (
        <div className="flex flex-col gap-2 border-t border-subtle bg-elevated p-3">
          {remaining.map((a) => (
            <BankTxnImportRow
              key={a.id}
              approval={a}
              workspaceId={workspaceId}
              canDecide={canDecide}
              categories={categories}
              batchMode={false}
              selected={false}
              onToggleSelect={() => {}}
              onDecided={(id) => {
                setDecidedInGroup((prev) => new Set([...prev, id]));
                onDecided(id);
              }}
            />
          ))}
        </div>
      )}

      {/* "Group is empty now" hint after decisions */}
      {remainingCount === 0 && (
        <div className="border-t border-subtle px-4 py-3 text-xs text-ink-tertiary">
          All rows in this group have been decided.
        </div>
      )}

      {/* Confirmation modal */}
      {confirmOpen && (
        <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/60 p-4">
          <div className="w-full max-w-md rounded-2xl border border-default bg-surface p-6 shadow-xl">
            <div className="flex items-start gap-3">
              <AlertTriangle
                size={20}
                className={group.cpaFlag ? "text-amber-300" : "text-credit"}
              />
              <div className="flex-1">
                <h2 className="text-base font-semibold">
                  Approve {remainingCount} {group.category} row
                  {remainingCount !== 1 ? "s" : ""}?
                </h2>
                {overrideCategory ? (
                  <p className="mt-1 text-xs text-ink-secondary">
                    Category override:{" "}
                    <span className="font-medium text-ink-primary">
                      {overrideCategory}
                    </span>{" "}
                    will be applied to every row.
                  </p>
                ) : (
                  <p className="mt-1 text-xs text-ink-secondary">
                    Each row posts under its suggested category ({group.category}).
                  </p>
                )}
                {group.cpaFlag && (
                  <p className="mt-2 rounded bg-amber-500/10 p-2 text-[11px] text-amber-300">
                    <AlertTriangle size={10} className="mr-1 inline" />
                    {group.cpaFlag}
                  </p>
                )}
                <p className="mt-2 text-xs text-ink-tertiary">
                  Total: inflow {centsToDollars(group.inflowCents)} · outflow{" "}
                  {centsToDollars(group.outflowCents)}.
                </p>
                {remaining.length > 0 && (
                  <div className="mt-3">
                    <p className="mb-1 text-[10px] uppercase tracking-wider text-ink-tertiary">
                      Sample ({Math.min(5, remaining.length)} of {remaining.length})
                    </p>
                    <ul className="space-y-0.5">
                      {remaining.slice(0, 5).map((a) => (
                        <li
                          key={a.id}
                          className="truncate font-mono text-[11px] text-ink-secondary"
                        >
                          {a.payload?.effective_date}{" "}
                          {a.payload?.description ?? "(no description)"}
                        </li>
                      ))}
                    </ul>
                  </div>
                )}
              </div>
            </div>
            <div className="mt-5 flex justify-end gap-2">
              <button
                onClick={() => setConfirmOpen(false)}
                disabled={running}
                className="inline-flex h-8 items-center rounded-md border border-subtle bg-surface px-3 text-xs text-ink-secondary hover:bg-elevated disabled:opacity-50"
              >
                Cancel
              </button>
              <button
                onClick={approveAll}
                disabled={running}
                className={cn(
                  "inline-flex h-8 items-center gap-1.5 rounded-md px-3 text-xs font-medium disabled:opacity-50",
                  group.cpaFlag
                    ? "bg-amber-500/20 text-amber-200 hover:bg-amber-500/30"
                    : "bg-credit/15 text-credit hover:bg-credit/25"
                )}
              >
                {running && <Loader2 size={12} className="animate-spin" />}
                Approve all
              </button>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}

// -----------------------------------------------------------------------
// Main grouped view
// -----------------------------------------------------------------------

export function BankTxnGroupedView({
  approvals,
  workspaceId,
  canDecide,
  categories,
  onDecided
}: {
  approvals: Approval[];
  workspaceId: string;
  canDecide: boolean;
  categories: string[];
  onDecided: (id: string) => void;
}) {
  const groups = useMemo(() => groupByCategory(approvals), [approvals]);

  if (groups.length === 0) {
    return (
      <div className="rounded-xl border border-default bg-surface px-5 py-8 text-center">
        <p className="text-sm text-ink-secondary">
          Nothing to review — books are caught up.
        </p>
      </div>
    );
  }

  const totalRows = approvals.length;
  const totalGroups = groups.length;
  const flaggedGroups = groups.filter((g) => g.cpaFlag).length;
  const uncategorizedCount =
    groups.find((g) => g.category === UNCATEGORIZED_KEY)?.approvals.length ??
    0;

  return (
    <div className="flex flex-col gap-3">
      <div className="rounded-xl border border-default bg-elevated px-4 py-2.5 text-xs text-ink-secondary">
        <span className="font-medium text-ink-primary">{totalRows}</span> rows
        in <span className="font-medium text-ink-primary">{totalGroups}</span>{" "}
        categories
        {flaggedGroups > 0 && (
          <span className="ml-2 text-amber-300">
            · {flaggedGroups} flagged for CPA review
          </span>
        )}
        {uncategorizedCount > 0 && (
          <span className="ml-2 text-amber-300">
            · {uncategorizedCount} uncategorized
          </span>
        )}
      </div>

      <div className="flex flex-col gap-2">
        {groups.map((g) => (
          <GroupCard
            key={g.category}
            group={g}
            workspaceId={workspaceId}
            canDecide={canDecide}
            categories={categories}
            onDecided={onDecided}
          />
        ))}
      </div>
    </div>
  );
}
