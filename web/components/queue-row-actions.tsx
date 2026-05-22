"use client";

import { useRouter } from "next/navigation";
import { useState, useTransition } from "react";
import { Check, Loader2, MessageCircle, X } from "lucide-react";
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

export function QueueRowActions({
  workspaceId,
  transaction,
  kind
}: {
  workspaceId: string;
  transaction: LiveTransaction;
  kind: "review" | "unknown" | "auto";
}) {
  const router = useRouter();
  const [pending, startTransition] = useTransition();
  const [result, setResult] = useState<ApiResponse | null>(null);
  const [error, setError] = useState<string | null>(null);

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
        const res = await fetch(
          `/api/workspaces/${workspaceId}/journal-entries`,
          {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({
              entity_id: transaction.entity_id,
              source: "simplefin",
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
          }
        );
        if (!res.ok) {
          throw new Error(`HTTP ${res.status}`);
        }
        const body = (await res.json()) as ApiResponse;
        setResult(body);
        if (body.ok || body.outcome === "blocked_idempotent") {
          router.refresh();
        }
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
          disabled
          className="inline-flex h-7 items-center justify-center rounded-md border border-subtle bg-surface px-2 text-ink-secondary opacity-50"
          title="Reject (coming next)"
        >
          <X size={12} />
        </button>
        <button
          disabled
          className="inline-flex h-7 items-center justify-center rounded-md border border-subtle bg-surface px-2 text-ink-secondary opacity-50"
          title="Ask agent (coming next)"
        >
          <MessageCircle size={12} />
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
