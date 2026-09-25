"use client";

/**
 * CategorizeAction — human-in-the-loop re-categorization for a single posted
 * journal entry (/journal/[id]).
 *
 * Calls the entity-scoped backend write POST
 * /api/workspaces/{wid}/entities/{eid}/transactions/{id}/categorize (app/api/tools.py:196)
 * through the Next.js proxy at
 * /api/workspaces/[workspaceId]/entities/[entityId]/transactions/[id]/categorize,
 * which mirrors the existing close-period / decide proxy pattern (bearer
 * forwarded server-side, body passed through, response relayed as-is).
 *
 * The backend can return three outcomes — shown honestly, not collapsed to a
 * generic "success":
 *   - "applied": correction posted immediately (under-materiality).
 *   - "queued_for_approval": correction WILL NOT take effect until an admin
 *     attests it on /approvals (over-materiality, or the caller is a
 *     bookkeeper — force_queue in categorize_transaction).
 *   - "noop": already this category, nothing written.
 * A refusal (ok:false, e.g. category not mapped, entry not posted) surfaces
 * its `reason` verbatim.
 */

import { useRouter } from "next/navigation";
import { useState, useTransition } from "react";
import { Check, ChevronDown, Clock, Loader2 } from "lucide-react";
import { cn } from "@/lib/cn";
import { CATEGORY_OPTIONS } from "@/lib/categories";

type CategorizeResponse = {
  ok: boolean;
  outcome: "applied" | "queued_for_approval" | "noop" | string;
  id?: string;
  from_category?: string;
  to_category?: string;
  category?: string;
  materiality_tier?: "L" | "M" | "H";
  approval_id?: string;
  reversal_entry_id?: string;
  restated_entry_id?: string;
  reason?: string;
  detail?: string;
};

export function CategorizeAction({
  workspaceId,
  entityId,
  transactionId,
  currentCategory,
  canCategorize
}: {
  workspaceId: string;
  entityId: string;
  transactionId: string;
  currentCategory: string | null;
  canCategorize: boolean;
}) {
  const router = useRouter();
  const [open, setOpen] = useState(false);
  const [category, setCategory] = useState("");
  const [reasoning, setReasoning] = useState("");
  const [pending, startTransition] = useTransition();
  const [error, setError] = useState<string | null>(null);
  const [result, setResult] = useState<CategorizeResponse | null>(null);

  if (!canCategorize) {
    return (
      <span
        className="text-[11px] text-ink-tertiary"
        title="Categorizing requires a bookkeeper, accountant, or admin role."
      >
        Categorize (bookkeeper role required)
      </span>
    );
  }

  function submit() {
    if (!category) {
      setError("Pick a category");
      return;
    }
    if (!reasoning.trim()) {
      setError("A short reason is required for the audit trail");
      return;
    }
    setError(null);
    startTransition(async () => {
      try {
        const res = await fetch(
          `/api/workspaces/${workspaceId}/entities/${entityId}/transactions/${transactionId}/categorize`,
          {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ category, reasoning: reasoning.trim() })
          }
        );
        const body = (await res.json().catch(() => ({}))) as CategorizeResponse;
        if (!res.ok) {
          throw new Error(body.detail ?? body.reason ?? `HTTP ${res.status}`);
        }
        setResult(body);
        if (body.ok) {
          setOpen(false);
          router.refresh();
        }
      } catch (e) {
        setError(e instanceof Error ? e.message : "Unknown error");
      }
    });
  }

  if (result?.ok && result.outcome === "queued_for_approval") {
    return (
      <div className="flex items-center gap-1.5 rounded-md bg-pending/15 px-2.5 py-1.5 text-[11px] font-medium text-pending">
        <Clock size={12} />
        Queued for admin approval — category will not change until attested.
      </div>
    );
  }
  if (result?.ok && result.outcome === "applied") {
    return (
      <div className="flex items-center gap-1.5 rounded-md bg-credit/15 px-2.5 py-1.5 text-[11px] font-medium text-credit">
        <Check size={12} />
        Categorized as {result.to_category}
      </div>
    );
  }
  if (result?.ok && result.outcome === "noop") {
    return (
      <div className="flex items-center gap-1.5 rounded-md bg-white/5 px-2.5 py-1.5 text-[11px] text-ink-secondary">
        Already {currentCategory ?? "this category"} — nothing to change.
      </div>
    );
  }

  if (!open) {
    return (
      <button
        onClick={() => setOpen(true)}
        className="inline-flex h-8 items-center gap-1.5 rounded-md border border-default bg-surface px-3 text-xs font-medium text-ink-secondary hover:bg-elevated"
      >
        <ChevronDown size={13} />
        Categorize
      </button>
    );
  }

  return (
    <div className="flex w-full max-w-sm flex-col gap-2 rounded-md border border-default bg-surface p-3">
      <label className="flex flex-col gap-1 text-[10px] uppercase tracking-wider text-ink-tertiary">
        Category
        <select
          value={category}
          onChange={(e) => setCategory(e.target.value)}
          className="rounded border border-default bg-canvas px-2 py-1.5 text-xs text-ink-primary focus:border-brand-500 focus:outline-none"
        >
          <option value="">— pick a category —</option>
          {CATEGORY_OPTIONS.map((c) => (
            <option key={c} value={c} disabled={c === currentCategory}>
              {c}
              {c === currentCategory ? " (current)" : ""}
            </option>
          ))}
        </select>
      </label>
      <label className="flex flex-col gap-1 text-[10px] uppercase tracking-wider text-ink-tertiary">
        Reasoning (required — goes in the audit trail)
        <textarea
          value={reasoning}
          onChange={(e) => setReasoning(e.target.value)}
          rows={2}
          placeholder="Why this category?"
          className="resize-none rounded border border-default bg-canvas px-2 py-1.5 text-xs text-ink-primary placeholder:text-ink-disabled focus:border-brand-500 focus:outline-none"
        />
      </label>
      <p className="text-[10px] text-ink-tertiary">
        A material change (M/H) or a bookkeeper-initiated change queues to{" "}
        <span className="font-medium">/approvals</span> for an admin to
        attest — it does not take effect immediately.
      </p>
      {error && <span className="text-[10px] text-debit">{error}</span>}
      <div className="flex items-center gap-1.5">
        <button
          onClick={submit}
          disabled={pending}
          className={cn(
            "inline-flex h-7 items-center gap-1.5 rounded-md bg-brand-500 px-3 text-[11px] font-medium text-white hover:bg-brand-600 disabled:opacity-50",
            pending && "cursor-wait"
          )}
        >
          {pending ? <Loader2 size={11} className="animate-spin" /> : <Check size={12} />}
          Submit
        </button>
        <button
          onClick={() => {
            setOpen(false);
            setError(null);
          }}
          disabled={pending}
          className="inline-flex h-7 items-center rounded-md border border-default px-3 text-[11px] text-ink-tertiary hover:bg-elevated"
        >
          Cancel
        </button>
      </div>
    </div>
  );
}
