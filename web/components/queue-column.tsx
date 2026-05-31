"use client";

import { useRouter } from "next/navigation";
import { useState, useTransition, useRef, useCallback, useEffect } from "react";
import { Check, Loader2, X, ChevronDown, ChevronUp } from "lucide-react";
import { cn } from "@/lib/cn";
import type { LiveTransaction } from "@/lib/transactions";
import { QueueRowActions } from "@/components/queue-row-actions";

type Outcome =
  | "posted"
  | "queued_for_confirm"
  | "blocked_materiality"
  | "blocked_unmapped"
  | "blocked_unknown_category"
  | "blocked_idempotent";

type ApiResponse = {
  ok: boolean;
  outcome: Outcome;
  reason?: string;
  journal_entry_id?: string;
  materiality_tier?: "L" | "M" | "H";
};

function materialityFor(amountStr: string): "L" | "M" | "H" {
  const abs = Math.abs(parseFloat(amountStr));
  if (abs >= 1000) return "H";
  if (abs >= 75) return "M";
  return "L";
}

function shortDate(iso: string | null): string {
  if (!iso) return "\u2014";
  const d = new Date(iso);
  if (Number.isNaN(d.valueOf())) return "\u2014";
  return d.toLocaleDateString("en-US", { month: "short", day: "numeric" });
}

function formatUSD(n: number): string {
  return n.toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 });
}

type QueueTone = "pending" | "agent" | "credit";

const toneMap: Record<QueueTone, string> = {
  pending: "border-pending/40 bg-pending/[0.04]",
  agent: "border-agent-500/40 bg-agent-500/[0.04]",
  credit: "border-credit/30 bg-credit/[0.03]"
};

const toneDotMap: Record<QueueTone, string> = {
  pending: "bg-pending",
  agent: "bg-agent-500",
  credit: "bg-credit"
};

export function QueueColumnClient({
  title,
  tone,
  items,
  total,
  kind,
  workspaceId
}: {
  title: string;
  tone: QueueTone;
  items: LiveTransaction[];
  total: number;
  kind: "review" | "unknown" | "auto";
  workspaceId: string;
}) {
  const router = useRouter();
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [focusedIdx, setFocusedIdx] = useState<number>(-1);
  const [bulkPending, setBulkPending] = useState(false);
  const [bulkResult, setBulkResult] = useState<{ ok: number; skipped: number; failed: string[] } | null>(null);
  const [rejectingIdx, setRejectingIdx] = useState<number | null>(null);
  const cardRefs = useRef<HTMLDivElement[]>([]);
  const onShowRejectRef = useRef<(idx: number) => void>(() => {});
  onShowRejectRef.current = (idx: number) => { setRejectingIdx(idx); };

  const toggleRow = useCallback((id: string) => {
    setSelected((prev) => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  }, []);

  const toggleFocused = useCallback(() => {
    if (focusedIdx < 0 || focusedIdx >= items.length) return;
    toggleRow(items[focusedIdx].id);
  }, [focusedIdx, items, toggleRow]);

  const moveFocus = useCallback((direction: number) => {
    setFocusedIdx((prev) => {
      const base = prev < 0 ? 0 : prev;
      const next = Math.max(0, Math.min(items.length - 1, base + direction));
      return next;
    });
  }, [items.length]);

  const confirmSelected = useCallback(async () => {
    if (selected.size === 0) return;
    setBulkPending(true);
    setBulkResult(null);
    const okList: string[] = [];
    const skipped: number[] = [];
    const failed: string[] = [];

    for (const txn of items) {
      if (!selected.has(txn.id)) continue;
      const mat = materialityFor(txn.amount);
      if (mat === "H") {
        skipped.push(items.indexOf(txn));
        continue;
      }
      if (!txn.entity_id || !txn.category || txn.category === "Uncategorized") {
        failed.push(`${txn.payee ?? txn.description}: needs entity mapping + category`);
        continue;
      }
      try {
        const amountCents = Math.round(parseFloat(txn.amount) * 100);
        const effective = txn.posted_at?.slice(0, 10) ?? new Date().toISOString().slice(0, 10);
        const res = await fetch(`/api/workspaces/${workspaceId}/journal-entries`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            entity_id: txn.entity_id,
            source: txn.org_name?.toLowerCase().includes("ebay") ? "ebay" : "simplefin",
            source_external_id: txn.id,
            effective_date: effective,
            amount_cents: amountCents,
            description: txn.description || txn.payee || "(no description)",
            payee: txn.payee,
            memo: txn.memo,
            category: txn.category,
            category_status: txn.category_status ?? "suggest",
            sign_matched: txn.category_sign_matched ?? true,
            external_account_id: txn.account_id,
            user_confirmed: true,
            reasoning_trace: `Bulk-confirmed via /reconciliation-queue (${selected.size} items)`,
            model_version: "vendor_rules:tier1"
          })
        });
        const body: ApiResponse = await res.json();
        if (body.ok || body.outcome === "blocked_idempotent") {
          okList.push(txn.id);
        } else {
          failed.push(`${txn.payee ?? txn.description}: ${body.outcome} — ${body.reason ?? ""}`);
        }
      } catch {
        failed.push(`${txn.payee ?? txn.description}: network error`);
      }
    }

    setBulkResult({ ok: okList.length, skipped: skipped.length, failed });
    if (okList.length > 0) router.refresh();
    setBulkPending(false);

    // Deselect only the successfully posted items
    setSelected((prev) => {
      const next = new Set(prev);
      for (const id of okList) next.delete(id);
      return next;
    });
  }, [selected, items, workspaceId, router]);

  // Keyboard handler
  useEffect(() => {
    const handler = (e: KeyboardEvent) => {
      // Ignore if typing in an input
      const tag = (e.target as HTMLElement)?.tagName;
      if (tag === "INPUT" || tag === "TEXTAREA" || tag === "SELECT") return;

      if (e.key === "j" || e.key === "ArrowDown") {
        e.preventDefault();
        moveFocus(1);
      } else if (e.key === "k" || e.key === "ArrowUp") {
        e.preventDefault();
        moveFocus(-1);
      } else if (e.key === "x" || e.key === "X") {
        e.preventDefault();
        toggleFocused();
      } else      if (e.key === "c" || e.key === "C") {
        e.preventDefault();
        if (selected.size > 0) confirmSelected();
      } else if (e.key === "r" || e.key === "R") {
        e.preventDefault();
        if (focusedIdx >= 0 && focusedIdx < items.length) {
          setRejectingIdx(focusedIdx);
        }
      } else if (e.key === "Escape") {
        e.preventDefault();
        setRejectingIdx(null);
      }
    };
    window.addEventListener("keydown", handler);
    return () => window.removeEventListener("keydown", handler);
  }, [moveFocus, toggleFocused, selected.size, confirmSelected]);

  // Scroll focused card into view
  useEffect(() => {
    if (focusedIdx >= 0 && focusedIdx < items.length) {
      cardRefs.current[focusedIdx]?.scrollIntoView({ block: "nearest", behavior: "smooth" });
    }
  }, [focusedIdx, items.length]);

  const hasMaterialityH = items.some((t) => materialityFor(t.amount) === "H");

  return (
    <div>
      <div className="mb-3 flex items-center gap-2">
        <span className={`h-1.5 w-1.5 rounded-full ${toneDotMap[tone]}`} />
        <h2 className="text-sm font-semibold">{title}</h2>
        <span className="ml-auto rounded-full bg-white/5 px-2 py-0.5 font-mono text-[10px] text-ink-tertiary">
          {items.length < total ? `${items.length}/${total}` : total}
        </span>
      </div>

      {selected.size > 0 && (
        <div className="mb-3 flex items-center gap-2 rounded-lg border border-brand-500/30 bg-brand-500/[0.04] px-3 py-2">
          <span className="text-xs text-brand-300 font-medium">
            {selected.size} selected
          </span>
          <button
            onClick={confirmSelected}
            disabled={bulkPending}
            className={cn(
              "inline-flex h-6 items-center gap-1 rounded-md bg-brand-500 px-2.5 text-[11px] font-medium text-white hover:bg-brand-600 disabled:opacity-50",
              bulkPending && "cursor-wait"
            )}
          >
            {bulkPending ? (
              <Loader2 size={10} className="animate-spin" />
            ) : (
              <Check size={10} />
            )}
            Confirm ({selected.size})
          </button>
          {hasMaterialityH && (
            <span className="text-[10px] text-pending">
              (items &gt;$1k skipped)
            </span>
          )}
          <button
            onClick={() => setSelected(new Set())}
            className="ml-auto text-[10px] text-ink-tertiary hover:text-ink-secondary"
          >
            Clear
          </button>
        </div>
      )}

      {bulkResult && (
        <div className={cn(
          "mb-3 rounded-lg border px-3 py-2 text-xs",
          bulkResult.failed.length > 0
            ? "border-pending/40 bg-pending/[0.04]"
            : "border-credit/40 bg-credit/[0.04]"
        )}>
          <span className="text-credit font-medium">{bulkResult.ok} posted</span>
          {bulkResult.skipped > 0 && (
            <span className="ml-2 text-pending">{bulkResult.skipped} skipped (materiality &gt;$1k)</span>
          )}
          {bulkResult.failed.length > 0 && (
            <div className="mt-1 text-debit">
              {bulkResult.failed.slice(0, 3).map((f, i) => (
                <div key={i}>{f}</div>
              ))}
              {bulkResult.failed.length > 3 && (
                <div className="text-ink-tertiary">…and {bulkResult.failed.length - 3} more</div>
              )}
            </div>
          )}
        </div>
      )}

      {items.length === 0 ? (
        <div className="rounded-lg border border-dashed border-default bg-surface p-6">
          <p className="text-xs text-ink-tertiary">
            {kind === "review" && "Nothing needs your attention right now."}
            {kind === "unknown" && "Every transaction matched a vendor rule."}
            {kind === "auto" && "No auto-categorized transactions yet."}
          </p>
        </div>
      ) : (
        <div className="space-y-3">
          {items.map((t, idx) => {
            const amt = parseFloat(t.amount);
            const isCredit = amt >= 0;
            const mat = materialityFor(t.amount);
            const isSelected = selected.has(t.id);
            const isFocused = idx === focusedIdx;
            const isMaterialityH = mat === "H";

            return (
              <div
                key={t.id}
                ref={(el) => { cardRefs.current[idx] = el!; }}
                className={cn(
                  "group relative rounded-lg border border-default bg-surface transition-all",
                  toneMap[tone],
                  isSelected && "border-brand-500/50 ring-1 ring-brand-500/20",
                  isFocused && "border-brand-400 shadow-sm",
                  isMaterialityH && "opacity-60",
                  "cursor-pointer hover:border-strong"
                )}
                onClick={() => toggleRow(t.id)}
              >
                {/* Checkbox */}
                <div className="absolute left-3 top-3">
                  <div className={cn(
                    "flex h-4 w-4 items-center justify-center rounded border transition-all",
                    isSelected
                      ? "border-brand-500 bg-brand-500 text-white"
                      : "border-default bg-surface group-hover:border-brand-400/50"
                  )}>
                    {isSelected && <Check size={10} />}
                  </div>
                </div>

                <div className="flex items-start justify-between gap-2 pl-8 pr-3 pt-3">
                  <h3 className="max-w-[200px] truncate text-sm font-semibold">
                    {t.payee ?? t.description ?? "\u2014"}
                  </h3>
                  <span
                    className={cn(
                      "shrink-0 font-mono text-sm tabular-nums",
                      isCredit ? "text-credit" : "text-ink-primary"
                    )}
                  >
                    {isCredit ? "+" : "-"}
                    {formatUSD(Math.abs(amt))}
                  </span>
                </div>
                <p className="mt-2 line-clamp-2 pl-8 pr-3 text-xs leading-relaxed text-ink-secondary">
                  {t.org_name} \u00b7 {t.account_name} \u00b7 {shortDate(t.posted_at)}
                </p>
                <div className="mt-2 flex flex-wrap items-center gap-1.5 pl-8 pr-3">
                  {t.category && t.category !== "Uncategorized" && (
                    <span
                      className={cn(
                        "inline-flex items-center rounded-full px-2 py-0.5 text-[10px] font-medium",
                        t.category_sign_matched === false
                          ? "bg-pending/15 text-pending"
                          : "bg-brand-500/10 text-brand-300"
                      )}
                    >
                      {t.category}
                    </span>
                  )}
                  {t.entity_display_name && (
                    <span className="inline-flex items-center rounded-full bg-ink-tertiary/10 px-2 py-0.5 text-[10px] text-ink-secondary">
                      {t.entity_display_name}
                    </span>
                  )}
                  {!t.entity_display_name && (
                    <span className="inline-flex items-center rounded-full bg-pending/10 px-2 py-0.5 text-[10px] text-pending">
                      Unmapped
                    </span>
                  )}
                  {isMaterialityH && (
                    <span className="inline-flex items-center rounded-full bg-pending/10 px-2 py-0.5 text-[10px] text-pending">
                      &gt;$1k
                    </span>
                  )}
                </div>
                <div className="mt-3 flex items-center justify-between pl-8 pr-3">
                  <div className="flex items-center gap-2 text-[11px] text-ink-tertiary">
                    <span className="inline-flex items-center rounded-full bg-ink-tertiary/10 px-1.5 py-0.5 text-[10px] text-ink-secondary">
                      j/k nav
                    </span>
                    <span className="inline-flex items-center rounded-full bg-ink-tertiary/10 px-1.5 py-0.5 text-[10px] text-ink-secondary">
                      x select
                    </span>
                    {isMaterialityH && (
                      <span className="text-pending">bulk-skip</span>
                    )}
                  </div>
                  <div className="pointer-events-auto" onClick={(e) => e.stopPropagation()}>
                    <QueueRowActions
                      workspaceId={workspaceId}
                      transaction={t}
                      kind={kind}
                      onShowReject={() => setRejectingIdx(idx)}
                    />
                  </div>
                </div>
              </div>
            );
          })}
        </div>
      )}
    </div>
  );
}
