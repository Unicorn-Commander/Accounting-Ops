"use client";

import { useState, useTransition } from "react";
import { Check, X, Loader2, TrendingUp, TrendingDown } from "lucide-react";
import { cn } from "@/lib/cn";
import { MaterialityBadge, StatusChip } from "@/components/primitives";
import type { Approval } from "@/lib/approvals";

// -----------------------------------------------------------------------
// Helpers
// -----------------------------------------------------------------------

function formatCents(cents: number): string {
  const abs = Math.abs(cents) / 100;
  return abs.toLocaleString("en-US", {
    minimumFractionDigits: 2,
    maximumFractionDigits: 2
  });
}

function categorizationBadge(source: string | undefined): React.ReactNode {
  if (!source) return <StatusChip tone="ink">Uncategorized</StatusChip>;
  if (source.startsWith("llm:"))
    return <StatusChip tone="agent">AI</StatusChip>;
  if (source.startsWith("vendor_rule") || source === "vendor_rules")
    return <StatusChip tone="ink">Vendor rule</StatusChip>;
  if (source === "human_override")
    return <StatusChip tone="credit">Human</StatusChip>;
  return <StatusChip tone="ink">{source}</StatusChip>;
}

// -----------------------------------------------------------------------
// Types
// -----------------------------------------------------------------------

export type RowDecision = {
  approvalId: string;
  decision: "approve" | "reject";
  categoryOverride?: string;
};

// -----------------------------------------------------------------------
// BankTxnImportRow
// Used both as standalone card (single-action mode) and as a selectable row
// inside the batch list.
// -----------------------------------------------------------------------

export function BankTxnImportRow({
  approval,
  workspaceId,
  canDecide,
  categories,
  // batch mode props
  batchMode = false,
  selected = false,
  onToggleSelect,
  onDecided
}: {
  approval: Approval;
  workspaceId: string;
  canDecide: boolean;
  categories: string[];
  batchMode?: boolean;
  selected?: boolean;
  onToggleSelect?: (id: string) => void;
  onDecided?: (id: string) => void;
}) {
  const p = approval.payload ?? {};
  const amountCents = typeof p.amount_cents === "number" ? p.amount_cents : 0;
  const isOutflow = amountCents < 0;
  const tier = (p.materiality_tier ?? "") as "L" | "M" | "H" | "";

  const [categoryOverride, setCategoryOverride] = useState<string>("");
  const [done, setDone] = useState<"approved" | "rejected" | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [pending, startTransition] = useTransition();

  function decide(decision: "approve" | "reject") {
    setError(null);
    startTransition(async () => {
      try {
        const body: Record<string, unknown> = { decision };
        if (decision === "approve" && categoryOverride) {
          body.category_override = categoryOverride;
        }
        const res = await fetch(
          `/api/workspaces/${workspaceId}/approvals/${approval.id}/decide`,
          {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify(body)
          }
        );
        const json = (await res.json().catch(() => ({}))) as {
          ok?: boolean;
          decision?: "approved" | "rejected";
          detail?: string;
        };
        if (!res.ok) throw new Error(json.detail ?? `HTTP ${res.status}`);
        const finalDecision =
          json.decision ?? (decision === "approve" ? "approved" : "rejected");
        setDone(finalDecision);
        onDecided?.(approval.id);
      } catch (e) {
        setError(e instanceof Error ? e.message : "Unknown error");
      }
    });
  }

  if (done) {
    return (
      <div className="flex items-center justify-between rounded-lg border border-default bg-surface px-4 py-3 opacity-60">
        <span className="font-mono text-xs text-ink-tertiary">
          {p.source_external_id}
        </span>
        <span
          className={cn(
            "inline-flex items-center gap-1 rounded-full px-2.5 py-1 text-[11px] font-medium",
            done === "approved"
              ? "bg-credit/15 text-credit"
              : "bg-ink-tertiary/15 text-ink-secondary"
          )}
        >
          {done === "approved" ? <Check size={11} /> : <X size={11} />}
          {done === "approved" ? "Approved" : "Rejected"}
        </span>
      </div>
    );
  }

  return (
    <div
      className={cn(
        "rounded-lg border bg-surface transition",
        selected ? "border-brand-500/60 bg-brand-500/[0.04]" : "border-default"
      )}
    >
      <div className="flex items-start gap-3 px-4 py-3">
        {/* Batch checkbox */}
        {batchMode && onToggleSelect && (
          <input
            type="checkbox"
            checked={selected}
            onChange={() => onToggleSelect(approval.id)}
            className="mt-1 h-4 w-4 shrink-0 cursor-pointer accent-brand-500"
            aria-label="Select transaction"
          />
        )}

        {/* Main content */}
        <div className="min-w-0 flex-1">
          {/* Top row: date + description + amount */}
          <div className="flex flex-wrap items-center gap-x-3 gap-y-1">
            {p.effective_date && (
              <span className="font-mono text-xs text-ink-tertiary">
                {p.effective_date}
              </span>
            )}
            <span className="truncate text-sm font-medium text-ink-primary">
              {p.description ?? p.source_external_id ?? approval.id}
            </span>
            <span
              className={cn(
                "ml-auto font-mono text-sm font-semibold tabular-nums",
                isOutflow ? "text-debit" : "text-credit"
              )}
            >
              {isOutflow ? (
                <TrendingDown size={12} className="mr-0.5 inline" />
              ) : (
                <TrendingUp size={12} className="mr-0.5 inline" />
              )}
              {isOutflow ? "-" : "+"}${formatCents(amountCents)}
            </span>
          </div>

          {/* Second row: badges */}
          <div className="mt-1.5 flex flex-wrap items-center gap-1.5">
            {tier && (tier === "L" || tier === "M" || tier === "H") && (
              <MaterialityBadge level={tier} />
            )}
            {p.category ? (
              <StatusChip tone="ink">{p.category}</StatusChip>
            ) : (
              <StatusChip tone="pending">Uncategorized</StatusChip>
            )}
            {categorizationBadge(p.categorization_source)}
            {approval.entity_display_name && (
              <StatusChip tone="ink">{approval.entity_display_name}</StatusChip>
            )}
          </div>

          {/* Account hints */}
          {(p.debit_account_hint || p.credit_account_hint) && (
            <div className="mt-1.5 flex flex-wrap items-center gap-2 text-[11px] text-ink-tertiary">
              {p.debit_account_hint && (
                <span>
                  <span className="text-debit">DR</span>{" "}
                  {p.debit_account_hint}
                </span>
              )}
              {p.credit_account_hint && (
                <span>
                  <span className="text-credit">CR</span>{" "}
                  {p.credit_account_hint}
                </span>
              )}
            </div>
          )}

          {/* Source external ID */}
          {p.source_external_id && (
            <p className="mt-1 font-mono text-[10px] text-ink-tertiary">
              {p.source_external_id}
            </p>
          )}
        </div>
      </div>

      {/* Action row — only shown in non-batch mode, or when row is selected in batch mode */}
      {canDecide && !batchMode && (
        <div className="flex flex-wrap items-center gap-2 border-t border-default px-4 py-2.5">
          {/* Category override dropdown */}
          {categories.length > 0 && (
            <select
              value={categoryOverride}
              onChange={(e) => setCategoryOverride(e.target.value)}
              disabled={pending}
              className="h-7 min-w-0 flex-1 rounded border border-subtle bg-elevated px-2 text-xs text-ink-primary focus:outline-none focus:ring-1 focus:ring-brand-500 disabled:opacity-50"
              aria-label="Override category"
            >
              <option value="">Keep suggested category</option>
              {categories.map((c) => (
                <option key={c} value={c}>
                  {c}
                </option>
              ))}
            </select>
          )}

          <button
            onClick={() => decide("approve")}
            disabled={pending}
            className={cn(
              "inline-flex h-7 items-center gap-1 rounded-md bg-credit/15 px-3 text-xs font-medium text-credit hover:bg-credit/25 disabled:opacity-50",
              pending && "cursor-wait"
            )}
          >
            {pending ? (
              <Loader2 size={11} className="animate-spin" />
            ) : (
              <Check size={12} />
            )}
            Approve
          </button>
          <button
            onClick={() => decide("reject")}
            disabled={pending}
            className="inline-flex h-7 items-center gap-1 rounded-md border border-subtle bg-surface px-3 text-xs text-ink-secondary hover:bg-elevated disabled:opacity-50"
          >
            <X size={12} /> Reject
          </button>

          {error && (
            <span className="text-[10px] text-debit" title={error}>
              {error}
            </span>
          )}
        </div>
      )}

      {!canDecide && !batchMode && (
        <div className="border-t border-default px-4 py-2">
          <span className="text-[11px] text-ink-tertiary">
            Admin approval required
          </span>
        </div>
      )}
    </div>
  );
}
