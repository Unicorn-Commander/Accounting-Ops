"use client";

/**
 * AiSuggestCategoriesPanel — human trigger for Tier-2 LLM categorization over
 * the Uncategorized pending approval-queue rows for one entity.
 *
 * Calls the entity-scoped backend write POST
 * /api/workspaces/{wid}/entities/{eid}/approvals/ai-categorize (app/api/tools.py:215,
 * params limit/year/dry_run) through the Next.js proxy at
 * /api/workspaces/[workspaceId]/entities/[entityId]/approvals/ai-categorize,
 * mirroring the existing close-period / decide proxy pattern.
 *
 * Flow: Preview (dry_run=true) runs the model and shows what it WOULD
 * suggest without writing anything. Confirm (dry_run=false) writes the same
 * suggestions into approval_queue.payload as category_status='suggest' —
 * status stays 'pending', nothing auto-posts — then refreshes the queue.
 */

import { useRouter } from "next/navigation";
import { useState, useTransition } from "react";
import { Check, Loader2, Sparkles } from "lucide-react";
import { cn } from "@/lib/cn";

type AiCategorizeResult = {
  approval_id: string;
  suggested_category: string;
  source: string;
  reasoning: string | null;
};

type AiCategorizeResponse = {
  scanned: number;
  suggested: number;
  unchanged: number;
  errors: number;
  dry_run: boolean;
  model: string;
  endpoint: string;
  results: AiCategorizeResult[];
  detail?: string;
};

export function AiSuggestCategoriesPanel({
  workspaceId,
  entityId,
  canCategorize
}: {
  workspaceId: string;
  entityId?: string;
  canCategorize: boolean;
}) {
  const router = useRouter();
  const [open, setOpen] = useState(false);
  const [pending, startTransition] = useTransition();
  const [error, setError] = useState<string | null>(null);
  const [preview, setPreview] = useState<AiCategorizeResponse | null>(null);
  const [confirmed, setConfirmed] = useState<AiCategorizeResponse | null>(null);

  if (!canCategorize) return null;

  if (!entityId) {
    return (
      <div className="rounded-lg border border-subtle bg-elevated/40 px-4 py-2.5 text-[11px] text-ink-tertiary">
        AI-suggest categories runs one entity at a time — pick a specific
        entity (not consolidated) to use it.
      </div>
    );
  }

  function run(dryRun: boolean) {
    setError(null);
    startTransition(async () => {
      try {
        const res = await fetch(
          `/api/workspaces/${workspaceId}/entities/${entityId}/approvals/ai-categorize?limit=50&dry_run=${dryRun}`,
          { method: "POST" }
        );
        const body = (await res.json().catch(() => ({}))) as AiCategorizeResponse;
        if (!res.ok) {
          throw new Error(body.detail ?? `HTTP ${res.status}`);
        }
        if (dryRun) {
          setPreview(body);
          setConfirmed(null);
        } else {
          setConfirmed(body);
          setPreview(null);
          router.refresh();
        }
      } catch (e) {
        setError(e instanceof Error ? e.message : "Unknown error");
      }
    });
  }

  if (!open) {
    return (
      <button
        onClick={() => setOpen(true)}
        className="inline-flex items-center gap-1.5 rounded-md border border-default bg-surface px-3 py-1.5 text-xs font-medium text-ink-secondary hover:bg-elevated"
      >
        <Sparkles size={13} />
        AI-suggest categories
      </button>
    );
  }

  return (
    <div className="flex flex-col gap-3 rounded-lg border border-default bg-surface p-4">
      <div className="flex items-center justify-between">
        <h3 className="flex items-center gap-1.5 text-xs font-semibold uppercase tracking-wider text-ink-tertiary">
          <Sparkles size={13} className="text-brand-400" />
          AI-suggest categories
        </h3>
        <button
          onClick={() => {
            setOpen(false);
            setPreview(null);
            setConfirmed(null);
            setError(null);
          }}
          className="text-[11px] text-ink-tertiary hover:text-ink-primary"
        >
          Close
        </button>
      </div>

      <p className="text-[11px] leading-relaxed text-ink-secondary">
        Runs the Tier-2 model over Uncategorized pending rows for this entity
        and writes a <em>suggestion</em> — it never auto-posts. Every
        suggestion still needs a human to approve or override it in the
        queue below. Very low-confidence rows may be escalated to a
        separately configured frontier model, if your deployment enables
        that fallback.
      </p>

      {!preview && !confirmed && (
        <button
          onClick={() => run(true)}
          disabled={pending}
          className={cn(
            "inline-flex h-8 items-center justify-center gap-1.5 self-start rounded-md bg-brand-500 px-3 text-xs font-medium text-white hover:bg-brand-600 disabled:opacity-50",
            pending && "cursor-wait"
          )}
        >
          {pending ? <Loader2 size={12} className="animate-spin" /> : <Sparkles size={12} />}
          Preview suggestions
        </button>
      )}

      {error && <span className="text-[11px] text-debit">{error}</span>}

      {preview && (
        <div className="flex flex-col gap-2">
          <p className="text-[11px] text-ink-tertiary">
            {preview.scanned} scanned · {preview.suggested} would be
            suggested · {preview.unchanged} left uncategorized
            {preview.errors > 0 ? ` · ${preview.errors} errors` : ""} ·
            model {preview.model}
          </p>
          {preview.results.length > 0 && (
            <div className="max-h-64 overflow-y-auto rounded-md border border-subtle">
              <table className="w-full text-[11px]">
                <thead className="sticky top-0 bg-elevated text-[9px] uppercase tracking-wider text-ink-tertiary">
                  <tr>
                    <th className="px-2 py-1.5 text-left font-medium">Suggested category</th>
                    <th className="px-2 py-1.5 text-left font-medium">Source</th>
                    <th className="px-2 py-1.5 text-left font-medium">Reasoning</th>
                  </tr>
                </thead>
                <tbody>
                  {preview.results.map((r) => (
                    <tr key={r.approval_id} className="border-t border-subtle/60">
                      <td className="px-2 py-1.5 font-medium">{r.suggested_category}</td>
                      <td className="px-2 py-1.5 text-ink-tertiary">{r.source}</td>
                      <td className="max-w-[280px] truncate px-2 py-1.5 text-ink-secondary" title={r.reasoning ?? ""}>
                        {r.reasoning}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
          <p className="text-[10px] text-pending">
            Nothing has been written yet — these are still "Uncategorized" in
            the queue. Confirm to write them as suggestions (status stays
            pending; still needs approval).
          </p>
          <div className="flex items-center gap-1.5">
            <button
              onClick={() => run(false)}
              disabled={pending || preview.suggested === 0}
              className={cn(
                "inline-flex h-8 items-center gap-1.5 rounded-md bg-credit/15 px-3 text-xs font-medium text-credit hover:bg-credit/25 disabled:opacity-50",
                pending && "cursor-wait"
              )}
            >
              {pending ? <Loader2 size={12} className="animate-spin" /> : <Check size={12} />}
              Confirm — write {preview.suggested} suggestion{preview.suggested === 1 ? "" : "s"}
            </button>
            <button
              onClick={() => setPreview(null)}
              disabled={pending}
              className="inline-flex h-8 items-center rounded-md border border-default px-3 text-xs text-ink-tertiary hover:bg-elevated"
            >
              Discard
            </button>
          </div>
        </div>
      )}

      {confirmed && (
        <div className="flex items-center gap-1.5 rounded-md bg-credit/15 px-3 py-2 text-[11px] font-medium text-credit">
          <Check size={12} />
          Wrote {confirmed.suggested} suggestion{confirmed.suggested === 1 ? "" : "s"} to the queue
          — still pending human approval below.
        </div>
      )}
    </div>
  );
}
