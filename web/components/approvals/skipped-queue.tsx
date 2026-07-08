"use client";

// SkippedQueue — shows items the user previously skipped (durable, server-
// side state). Each item has an "Un-skip" button that flips its status back
// to 'pending' so it shows up in the active review flow.

import { useState } from "react";
import { useRouter } from "next/navigation";
import { Loader2, RotateCcw, SkipForward } from "lucide-react";
import { Card } from "@/components/primitives";
import { cn } from "@/lib/cn";
import type { Approval } from "@/lib/approvals";

export function SkippedQueue({
  approvals,
  workspaceId,
  canDecide,
}: {
  approvals: Approval[];
  workspaceId: string;
  canDecide: boolean;
}) {
  const router = useRouter();
  const [pendingId, setPendingId] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  async function unskip(approvalId: string) {
    if (pendingId) return;
    setPendingId(approvalId);
    setError(null);
    try {
      const res = await fetch(
        `/api/workspaces/${workspaceId}/approvals/${approvalId}/unskip`,
        {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ note: null }),
        }
      );
      const j = (await res.json().catch(() => ({}))) as { detail?: string };
      if (!res.ok) throw new Error(j.detail ?? `HTTP ${res.status}`);
      router.refresh();
    } catch (e) {
      setError(e instanceof Error ? e.message : "Un-skip failed");
    } finally {
      setPendingId(null);
    }
  }

  if (approvals.length === 0) {
    return (
      <Card>
        <div className="flex flex-col items-center gap-2 py-10 text-center">
          <SkipForward size={26} className="text-pending" />
          <p className="text-base font-medium text-ink-secondary">
            Nothing skipped right now.
          </p>
          <p className="max-w-md text-xs text-ink-tertiary">
            Use the "Skip" action on the Review tab to set items aside for
            later. They&apos;ll show up here until you un-skip or decide.
          </p>
        </div>
      </Card>
    );
  }

  return (
    <div className="flex flex-col gap-3">
      <div className="rounded-xl border border-pending/40 bg-pending/[0.06] px-5 py-3 text-xs text-pending">
        Skipped items do NOT post to the ledger. They stay out of the active
        queue until you decide. {approvals.length} item
        {approvals.length === 1 ? "" : "s"} waiting.
      </div>
      {error && (
        <div className="rounded-md border border-debit/40 bg-debit/[0.05] px-3 py-2 text-xs text-debit">
          {error}
        </div>
      )}
      <div className="flex flex-col gap-2">
        {approvals.map((a) => (
          <Card key={a.id}>
            <div className="flex flex-col gap-3 md:flex-row md:items-start md:justify-between">
              <div className="min-w-0 flex-1">
                <div className="flex flex-wrap items-center gap-2 text-sm">
                  <span className="font-semibold">{subjectLabel(a)}</span>
                  {a.entity_display_name && (
                    <span className="rounded-full bg-elevated px-2 py-0.5 text-[10px] text-ink-secondary">
                      {a.entity_display_name}
                    </span>
                  )}
                  {a.amount && (
                    <span className="font-mono text-xs text-ink-secondary">
                      {a.amount} {a.currency_code ?? "USD"}
                    </span>
                  )}
                  <span className="rounded-full bg-pending/15 px-2 py-0.5 text-[10px] font-semibold uppercase tracking-wider text-pending">
                    skipped
                  </span>
                </div>
                <p className="mt-2 text-sm leading-relaxed text-ink-secondary">
                  {a.reasoning}
                </p>
                <p className="mt-2 text-[11px] text-ink-tertiary">
                  Requested by{" "}
                  <span className="font-mono">{a.requested_by_identity}</span>{" "}
                  · {new Date(a.created_at).toLocaleString()}
                </p>
              </div>
              <div className="shrink-0">
                <button
                  type="button"
                  onClick={() => unskip(a.id)}
                  disabled={pendingId === a.id || !canDecide}
                  className={cn(
                    "inline-flex items-center gap-1.5 rounded-md bg-brand-500/15 px-3 py-1.5 text-xs font-medium text-brand-300 hover:bg-brand-500/25",
                    "disabled:opacity-50"
                  )}
                  aria-label={`Un-skip approval ${a.id}`}
                >
                  {pendingId === a.id ? (
                    <Loader2 size={12} className="animate-spin" />
                  ) : (
                    <RotateCcw size={12} />
                  )}
                  Un-skip
                </button>
              </div>
            </div>
          </Card>
        ))}
      </div>
    </div>
  );
}

function subjectLabel(a: Approval): string {
  if (a.subject_type === "journal_recategorization") return "Recategorization";
  if (a.subject_type === "compliance_rule") return "Compliance rule";
  if (a.subject_type === "bank_transaction_import") return "Bank import";
  return a.subject_type;
}
