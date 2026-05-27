"use client";

import { useRouter } from "next/navigation";
import { useState, useTransition } from "react";
import { Check, Loader2, X } from "lucide-react";
import { cn } from "@/lib/cn";
import type { LiveTransaction } from "@/lib/transactions";

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
  existing_entry_id?: string | null;
};

const CATEGORY_OPTIONS = [
  "Revenue · Marketplace",
  "Revenue · Music",
  "Revenue · Services",
  "Revenue · Subscriptions",
  "SaaS · AI",
  "SaaS · Email",
  "SaaS · Productivity",
  "Infrastructure · CDN",
  "Infrastructure · VPS",
  "Infrastructure · Network",
  "Banking · Fees",
  "Banking · Interest",
  "Inter-entity transfer",
  "Travel · Lodging",
  "Travel · Transport",
  "Meals",
  "Office · Supplies",
  "Contractor · 1099-NEC"
];

function bestPattern(t: LiveTransaction): string {
  // Most distinctive token from the payee for the override pattern
  const text = (t.payee ?? t.description ?? "").toLowerCase();
  // Strip non-alphanumeric, find longest alpha word
  const tokens = text
    .replace(/[^a-z0-9\s]/g, " ")
    .split(/\s+/)
    .filter((w) => w.length >= 4 && !["card", "auto", "ach", "withdrawal", "deposit", "online", "from", "pmt", "the", "for"].includes(w));
  return tokens[0] ?? text.slice(0, 30);
}

export function QueueRowActions({
  workspaceId,
  transaction,
  kind,
  onShowReject
}: {
  workspaceId: string;
  transaction: LiveTransaction;
  kind: "review" | "unknown" | "auto";
  onShowReject?: () => void;
}) {
  const router = useRouter();
  const [pending, startTransition] = useTransition();
  const [result, setResult] = useState<ApiResponse | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [showReject, setShowReject] = useState(false);
  const [rejectCategory, setRejectCategory] = useState("");
  const [rejectPattern, setRejectPattern] = useState(bestPattern(transaction));

  async function confirmPost() {
    setError(null);
    setResult(null);
    if (!transaction.entity_id || !transaction.category || transaction.category === "Uncategorized") {
      setError("Need entity mapping + category before posting");
      return;
    }
    const amountCents = Math.round(parseFloat(transaction.amount) * 100);
    const effective = transaction.posted_at?.slice(0, 10) ?? new Date().toISOString().slice(0, 10);
    startTransition(async () => {
      try {
        const res = await fetch(`/api/workspaces/${workspaceId}/journal-entries`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            entity_id: transaction.entity_id,
            source: transaction.org_name?.toLowerCase().includes("ebay") ? "ebay" : "simplefin",
            source_external_id: transaction.id,
            effective_date: effective,
            amount_cents: amountCents,
            description: transaction.description || transaction.payee || "(no description)",
            payee: transaction.payee,
            memo: transaction.memo,
            category: transaction.category,
            category_status: transaction.category_status ?? "suggest",
            sign_matched: transaction.category_sign_matched ?? true,
            external_account_id: transaction.account_id,
            user_confirmed: true,
            reasoning_trace: `User-confirmed via /reconciliation-queue`,
            model_version: "vendor_rules:tier1"
          })
        });
        if (!res.ok) throw new Error(`HTTP ${res.status}`);
        const body = (await res.json()) as ApiResponse;
        setResult(body);
        if (body.ok || body.outcome === "blocked_idempotent") router.refresh();
      } catch (e) {
        setError(e instanceof Error ? e.message : "Unknown error");
      }
    });
  }

  async function saveRejectAndRecategorize() {
    if (!rejectCategory) {
      setError("Pick a category");
      return;
    }
    if (!rejectPattern || rejectPattern.length < 2) {
      setError("Pattern must be at least 2 chars");
      return;
    }
    setError(null);
    startTransition(async () => {
      try {
        const res = await fetch(`/api/workspaces/${workspaceId}/category-overrides`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            pattern: rejectPattern,
            category: rejectCategory,
            entity_id: transaction.entity_id ?? null
          })
        });
        if (!res.ok) {
          const body = await res.json().catch(() => ({ detail: `HTTP ${res.status}` }));
          throw new Error(body.detail ?? "Override save failed");
        }
        setShowReject(false);
        router.refresh();
      } catch (e) {
        setError(e instanceof Error ? e.message : "Unknown error");
      }
    });
  }

  if (kind === "auto") {
    return (
      <button
        onClick={confirmPost}
        disabled={pending}
        className={cn(
          "inline-flex h-7 items-center gap-1 rounded-md bg-credit/15 px-2 text-[11px] font-medium text-credit hover:bg-credit/25 disabled:opacity-50",
          pending && "cursor-wait"
        )}
        title="Post this entry to the journal"
      >
        {pending ? <Loader2 size={11} className="animate-spin" /> : <Check size={12} />}
        Post
      </button>
    );
  }

  if (showReject) {
    return (
      <div className="flex flex-col gap-1.5 rounded-md border border-pending/40 bg-pending/[0.04] p-2">
        <div className="text-[10px] text-ink-tertiary">
          Train future txns matching:
        </div>
        <input
          type="text"
          value={rejectPattern}
          onChange={(e) => setRejectPattern(e.target.value)}
          className="rounded border border-default bg-canvas px-2 py-1 text-[11px] focus:border-brand-500 focus:outline-none"
        />
        <select
          value={rejectCategory}
          onChange={(e) => setRejectCategory(e.target.value)}
          className="rounded border border-default bg-canvas px-2 py-1 text-[11px]"
        >
          <option value="">— pick category —</option>
          {CATEGORY_OPTIONS.map((c) => (
            <option key={c} value={c}>
              {c}
            </option>
          ))}
        </select>
        <div className="flex items-center gap-1">
          <button
            onClick={saveRejectAndRecategorize}
            disabled={pending || !rejectCategory}
            className="inline-flex h-6 items-center gap-1 rounded bg-brand-500 px-2 text-[10px] font-medium text-white hover:bg-brand-600 disabled:opacity-50"
          >
            {pending ? <Loader2 size={10} className="animate-spin" /> : null}
            Save rule
          </button>
          <button
            onClick={() => { setShowReject(false); }}
            className="inline-flex h-6 items-center rounded border border-default px-2 text-[10px] text-ink-tertiary hover:bg-elevated"
          >
            Cancel
          </button>
        </div>
        {error && <span className="text-[10px] text-debit">{error}</span>}
      </div>
    );
  }

  return (
    <div className="flex flex-col items-end gap-1">
      <div className="flex items-center gap-1">
        <button
          onClick={confirmPost}
          disabled={pending}
          className={cn(
            "inline-flex h-7 items-center gap-1 rounded-md bg-credit/15 px-2 text-[11px] font-medium text-credit hover:bg-credit/25 disabled:opacity-50",
            pending && "cursor-wait"
          )}
          title="Confirm + post to journal"
        >
          {pending ? <Loader2 size={11} className="animate-spin" /> : <Check size={12} />}
          Confirm
        </button>
       <button
            onClick={() => { setShowReject(true); onShowReject?.(); }}
            className="inline-flex h-7 items-center justify-center rounded-md border border-subtle bg-surface px-2 text-ink-secondary hover:bg-elevated"
            title="Reject + train a new category rule"
          >
          <X size={12} />
        </button>
      </div>
      {error && (
        <span className="max-w-[180px] truncate text-right text-[10px] text-debit" title={error}>
          {error}
        </span>
      )}
      {result && !result.ok && result.outcome !== "blocked_idempotent" && (
        <span
          className="max-w-[180px] truncate text-right text-[10px] text-pending"
          title={result.reason ?? result.outcome}
        >
          {result.outcome.replace(/^blocked_/, "")}
        </span>
      )}
      {result?.ok && (
        <span className="text-[10px] text-credit">posted · {result.materiality_tier}</span>
      )}
    </div>
  );
}
