"use client";

import { useRouter } from "next/navigation";
import { useState, useTransition } from "react";
import { Check, Loader2, X } from "lucide-react";
import { cn } from "@/lib/cn";
import { decideApproval } from "@/lib/approval-decide";
import type { ApiFailureKind } from "@/lib/api-error";

export function ApprovalActions({
  workspaceId,
  approvalId,
  canDecide
}: {
  workspaceId: string;
  approvalId: string;
  // Only admins (and owners) can decide; lower roles see a read-only note.
  canDecide: boolean;
}) {
  const router = useRouter();
  const [pending, startTransition] = useTransition();
  const [error, setError] = useState<{ kind: ApiFailureKind; message: string } | null>(null);
  const [done, setDone] = useState<"approved" | "rejected" | null>(null);

  function decide(decision: "approve" | "reject") {
    setError(null);
    startTransition(async () => {
      const result = await decideApproval(fetch, workspaceId, approvalId, decision);
      if (!result.ok) {
        setError({ kind: result.kind, message: result.message });
        return;
      }
      setDone(result.data.decision ?? (decision === "approve" ? "approved" : "rejected"));
      router.refresh();
    });
  }

  if (done) {
    return (
      <span
        className={cn(
          "inline-flex items-center gap-1 rounded-full px-2.5 py-1 text-[11px] font-medium",
          done === "approved" ? "bg-credit/15 text-credit" : "bg-ink-tertiary/15 text-ink-secondary"
        )}
      >
        {done === "approved" ? <Check size={11} /> : <X size={11} />}
        {done === "approved" ? "Approved + applied" : "Rejected"}
      </span>
    );
  }

  if (!canDecide) {
    return (
      <span className="text-[11px] text-ink-tertiary" title="Only an admin can attest a material action.">
        Admin approval required
      </span>
    );
  }

  return (
    <div className="flex flex-col items-end gap-1">
      <div className="flex items-center gap-1.5">
        <button
          onClick={() => decide("approve")}
          disabled={pending}
          className={cn(
            "inline-flex h-8 items-center gap-1.5 rounded-md bg-credit/15 px-3 text-xs font-medium text-credit hover:bg-credit/25 disabled:opacity-50",
            pending && "cursor-wait"
          )}
          title="Approve + apply this held action"
        >
          {pending ? <Loader2 size={12} className="animate-spin" /> : <Check size={13} />}
          Approve
        </button>
        <button
          onClick={() => decide("reject")}
          disabled={pending}
          className="inline-flex h-8 items-center gap-1.5 rounded-md border border-subtle bg-surface px-3 text-xs text-ink-secondary hover:bg-elevated disabled:opacity-50"
          title="Reject: leaves the ledger / rules untouched"
        >
          <X size={13} /> Reject
        </button>
      </div>
      {error && (
        <span
          role={error.kind === "retry" ? "status" : "alert"}
          className={cn(
            "max-w-[260px] text-right text-[10px]",
            error.kind === "retry" ? "text-pending" : "text-debit"
          )}
          title={error.message}
        >
          {error.message}
        </span>
      )}
    </div>
  );
}
