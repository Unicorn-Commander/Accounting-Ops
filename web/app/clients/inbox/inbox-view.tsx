"use client";

// InboxView — the cross-client CPA "Work Inbox" list + inline-act.
//
// Renders the merged, stalest-first list from GET /api/me/work-inbox
// (see web/lib/work-inbox.ts). The list crosses workspaces, so EVERY action
// must target the item's OWN workspace_id — never the active-workspace cookie.
//
// INLINE ACT (the throughput win)
// -------------------------------
// For item_type === 'approval' we render [Approve] / [Reject] that POST to the
// EXISTING per-workspace decide proxy:
//   /api/workspaces/{item.workspace_id}/approvals/{item.id}/decide
// That proxy (web/app/api/workspaces/[workspaceId]/approvals/[approvalId]/
// decide/route.ts) is fully workspace-parameterized and attaches the caller's
// Bearer server-side — it is NOT coupled to the active-workspace cookie — so
// inline-act works cross-workspace with no proxy changes. This mirrors the
// exact client fetch in app/approvals/recommendations-view.tsx.
//
// The decide endpoint's role floor is `admin` (require_role(workspace,
// "admin")), but the inbox's actor floor is `accountant`/`cpa`. The contract
// carries no per-item role, so we can't pre-gate the buttons; instead an
// inline approve that returns 403 surfaces a soft error pointing at the
// deep-link (open the workspace and decide there). On success we optimistically
// remove the row + decrement the header counts.
//
// For item_type === 'tax_review' we ALWAYS deep-link (resolution is nuanced —
// never inline it): cookie-swap to the item's workspace then router.push the
// backend-provided deep_link, mirroring clients-table.tsx gotoWorkspace().

import { useMemo, useState, useTransition } from "react";
import { useRouter } from "next/navigation";
import {
  AlertTriangle,
  ArrowRight,
  Check,
  FileText,
  Inbox,
  Loader2,
  ShieldAlert,
  X
} from "lucide-react";
import { Card, EmptyState } from "@/components/primitives";
import { cn } from "@/lib/cn";
import { formatAmount } from "@/lib/journal";
import {
  formatAge,
  type WorkInboxItem,
  type WorkInboxResponse
} from "@/lib/work-inbox";

// ---------------------------------------------------------------------------
// Badges
// ---------------------------------------------------------------------------

function ItemTypeBadge({ type }: { type: WorkInboxItem["item_type"] }) {
  const isApproval = type === "approval";
  return (
    <span
      className={cn(
        "inline-flex shrink-0 items-center gap-1 rounded-full px-2 py-0.5 text-[10px] font-semibold uppercase tracking-wider",
        isApproval
          ? "bg-brand-500/15 text-brand-400"
          : "bg-pending/15 text-pending"
      )}
    >
      {isApproval ? <Inbox size={9} /> : <FileText size={9} />}
      {isApproval ? "Approval" : "Tax review"}
    </span>
  );
}

// tax_review severity -> tone. Mirrors the backend's confidence->severity
// inversion (a LOW-confidence position needs the most human attention).
function SeverityBadge({ severity }: { severity: string }) {
  const s = severity.toLowerCase();
  const tone =
    s === "high"
      ? "bg-debit/15 text-debit"
      : s === "medium"
        ? "bg-pending/15 text-pending"
        : "bg-white/5 text-ink-secondary";
  return (
    <span
      className={cn(
        "inline-flex shrink-0 items-center rounded-full px-2 py-0.5 text-[10px] font-medium uppercase tracking-wider",
        tone
      )}
    >
      {s} attn
    </span>
  );
}

// approval recommended_decision -> tone. APPROVE leans credit, REJECT debit.
function RecommendationBadge({
  decision,
  confidence
}: {
  decision: string;
  confidence?: string | null;
}) {
  const d = decision.toLowerCase();
  const tone = d.includes("approv")
    ? "bg-credit/15 text-credit"
    : d.includes("reject")
      ? "bg-debit/15 text-debit"
      : "bg-white/5 text-ink-secondary";
  return (
    <span
      className={cn(
        "inline-flex shrink-0 items-center gap-1 rounded-full px-2 py-0.5 text-[10px] font-medium uppercase tracking-wider",
        tone
      )}
      title={
        confidence ? `AI recommends ${decision} (${confidence} confidence)` : undefined
      }
    >
      AI: {decision}
      {confidence ? <span className="opacity-70">· {confidence}</span> : null}
    </span>
  );
}

// ---------------------------------------------------------------------------
// Row
// ---------------------------------------------------------------------------

type RowState = "idle" | "busy" | "done";

function InboxRow({
  item,
  onDecided
}: {
  item: WorkInboxItem;
  /** Called after a successful inline decision so the parent can drop the row
   *  + adjust header counts. `decision` is the action that succeeded. */
  onDecided: (item: WorkInboxItem, decision: "approve" | "reject") => void;
}) {
  const router = useRouter();
  const [state, setState] = useState<RowState>("idle");
  const [error, setError] = useState<string | null>(null);
  const [, startTransition] = useTransition();

  const isApproval = item.item_type === "approval";

  // Deep-link (used for tax_review always, and as the fallback an approval
  // error points to). Cookie-swap to THIS item's workspace, then navigate —
  // identical mechanism to clients-table.tsx gotoWorkspace().
  function gotoDeepLink() {
    document.cookie = `aops_workspace=${item.workspace_id}; Path=/; SameSite=Lax; Max-Age=2592000`;
    document.cookie = `aops_entity=; Path=/; SameSite=Lax; Max-Age=0`;
    router.push(item.deep_link);
  }

  async function decide(decision: "approve" | "reject") {
    if (state === "busy") return;
    setState("busy");
    setError(null);
    try {
      const res = await fetch(
        `/api/workspaces/${item.workspace_id}/approvals/${item.id}/decide`,
        {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ decision })
        }
      );
      if (!res.ok) {
        const j = (await res.json().catch(() => ({}))) as { detail?: string };
        // 403 == this caller's role in THAT workspace can't attest (decide
        // floor is admin); 409 == someone already decided it. Either way,
        // keep the row and point the user at the workspace.
        const reason =
          res.status === 403
            ? "You can't attest in this client — open it to decide."
            : res.status === 409
              ? "Already decided elsewhere — open the client to refresh."
              : j.detail ?? `Couldn't ${decision} (HTTP ${res.status}).`;
        setError(reason);
        setState("idle");
        return;
      }
      // Optimistic: mark done, then let the parent drop us from the list.
      setState("done");
      startTransition(() => onDecided(item, decision));
    } catch (e) {
      setError(e instanceof Error ? e.message : "Network error — try again.");
      setState("idle");
    }
  }

  return (
    <li
      className={cn(
        "flex flex-col gap-2 px-4 py-3 transition md:flex-row md:items-center md:gap-4",
        state === "done"
          ? "pointer-events-none opacity-40"
          : "hover:bg-white/[0.02]"
      )}
    >
      {/* Title + meta */}
      <div className="min-w-0 flex-1">
        <div className="flex flex-wrap items-center gap-2">
          <ItemTypeBadge type={item.item_type} />
          <span className="truncate text-sm font-medium text-ink-primary">
            {item.title}
          </span>
        </div>
        <div className="mt-1 flex flex-wrap items-center gap-x-3 gap-y-1 text-[11px] text-ink-tertiary">
          {item.amount != null && item.amount !== "" && (
            <span className="font-mono text-ink-secondary">
              {formatAmount(item.amount, item.currency_code ?? "USD")}
            </span>
          )}
          {isApproval && item.recommended_decision ? (
            <RecommendationBadge
              decision={item.recommended_decision}
              confidence={item.confidence}
            />
          ) : null}
          {!isApproval && item.severity ? (
            <SeverityBadge severity={item.severity} />
          ) : null}
          <span title={item.created_at ?? undefined}>
            {formatAge(item.age_hours)} old
          </span>
        </div>
        {error && (
          <p className="mt-1.5 inline-flex items-center gap-1 text-[11px] text-debit">
            <AlertTriangle size={11} /> {error}
          </p>
        )}
      </div>

      {/* Actions */}
      <div className="flex shrink-0 items-center gap-2 md:ml-3">
        {isApproval ? (
          <>
            <button
              type="button"
              onClick={() => decide("approve")}
              disabled={state !== "idle"}
              className="inline-flex h-8 items-center gap-1.5 rounded-md bg-credit/15 px-3 text-xs font-medium text-credit transition hover:bg-credit/25 disabled:opacity-50"
              aria-label={`Approve: ${item.title}`}
            >
              {state === "busy" ? (
                <Loader2 size={12} className="animate-spin" />
              ) : (
                <Check size={12} />
              )}
              Approve
            </button>
            <button
              type="button"
              onClick={() => decide("reject")}
              disabled={state !== "idle"}
              className="inline-flex h-8 items-center gap-1.5 rounded-md border border-subtle bg-surface px-3 text-xs font-medium text-ink-secondary transition hover:bg-debit/10 hover:text-debit disabled:opacity-50"
              aria-label={`Reject: ${item.title}`}
            >
              <X size={12} />
              Reject
            </button>
            <button
              type="button"
              onClick={gotoDeepLink}
              className="inline-flex h-8 items-center gap-1 rounded-md px-2 text-xs text-ink-tertiary transition hover:text-brand-400"
              aria-label={`Open ${item.workspace_display_name} to review this item`}
              title="Open this client to review in context"
            >
              Open <ArrowRight size={12} />
            </button>
          </>
        ) : (
          // tax_review — always deep-link (resolution is nuanced).
          <button
            type="button"
            onClick={gotoDeepLink}
            className="inline-flex h-8 items-center gap-1.5 rounded-md bg-pending/15 px-3 text-xs font-medium text-pending transition hover:bg-pending/25"
            aria-label={`Review in ${item.workspace_display_name}`}
          >
            Review <ArrowRight size={12} />
          </button>
        )}
      </div>
    </li>
  );
}

// ---------------------------------------------------------------------------
// Main view
// ---------------------------------------------------------------------------

export function InboxView({ data }: { data: WorkInboxResponse }) {
  // Local copy so inline decisions can optimistically remove rows without a
  // server round-trip. We keep the API's stalest-first ORDER untouched.
  const [items, setItems] = useState<WorkInboxItem[]>(data.items);
  // Live header counts, decremented as approvals are decided inline.
  const [approvalCount, setApprovalCount] = useState<number>(
    data.totals.by_item_type.approval ?? 0
  );
  const [taxReviewCount] = useState<number>(
    data.totals.by_item_type.tax_review ?? 0
  );
  const [decidedCount, setDecidedCount] = useState(0);

  function handleDecided(decided: WorkInboxItem) {
    setItems((prev) => prev.filter((it) => it.id !== decided.id));
    if (decided.item_type === "approval") {
      setApprovalCount((c) => Math.max(0, c - 1));
    }
    setDecidedCount((c) => c + 1);
  }

  const total = approvalCount + taxReviewCount;
  // Distinct clients still represented in the (live) list.
  const clientCount = useMemo(
    () => new Set(items.map((it) => it.workspace_id)).size,
    [items]
  );

  // ---- All cleared (either nothing came back, or we cleared it inline) ----
  if (items.length === 0) {
    return (
      <EmptyState
        variant="no-data"
        icon={<Inbox size={28} className="text-credit" aria-hidden="true" />}
        title={
          decidedCount > 0
            ? "Inbox cleared. Nothing else waiting on you."
            : "No open items across your clients."
        }
        description={
          decidedCount > 0
            ? `You actioned ${decidedCount} item${decidedCount === 1 ? "" : "s"} from here.`
            : "Pending approvals and open tax-review items across every client you can act on land here, stalest first."
        }
      />
    );
  }

  return (
    <div className="flex flex-col gap-4">
      {/* Header strip — live counts that match what's on screen. */}
      <div className="flex flex-wrap items-center gap-x-4 gap-y-2 rounded-lg border border-default bg-elevated px-4 py-3">
        <div className="text-sm">
          <span className="font-semibold text-ink-primary">{total}</span>{" "}
          <span className="text-ink-secondary">
            item{total === 1 ? "" : "s"} across{" "}
          </span>
          <span className="font-semibold text-ink-primary">{clientCount}</span>{" "}
          <span className="text-ink-secondary">
            client{clientCount === 1 ? "" : "s"}
          </span>
        </div>
        <span className="hidden text-ink-tertiary sm:block">·</span>
        <div className="flex items-center gap-1.5">
          <span className="inline-flex items-center gap-1 rounded-full bg-brand-500/15 px-2 py-0.5 text-[10px] font-medium uppercase tracking-wider text-brand-400">
            <Inbox size={9} /> {approvalCount} approval
            {approvalCount === 1 ? "" : "s"}
          </span>
          <span className="inline-flex items-center gap-1 rounded-full bg-pending/15 px-2 py-0.5 text-[10px] font-medium uppercase tracking-wider text-pending">
            <FileText size={9} /> {taxReviewCount} review
            {taxReviewCount === 1 ? "" : "s"}
          </span>
        </div>
        <span className="ml-auto text-[10px] uppercase tracking-wider text-ink-tertiary">
          Stalest first
        </span>
      </div>

      {/* Truncation banner — NEVER hide the drop. */}
      {data.truncated && (
        <div className="flex items-start gap-2 rounded-lg border border-pending/40 bg-pending/[0.06] px-4 py-3 text-xs text-ink-secondary">
          <ShieldAlert size={14} className="mt-0.5 shrink-0 text-pending" />
          <p>
            Showing <span className="font-semibold text-ink-primary">{data.totals.total}</span>{" "}
            ·{" "}
            <span className="font-semibold text-pending">{data.dropped}</span>{" "}
            older item{data.dropped === 1 ? "" : "s"} not shown — raise the
            limit or open the busiest client to clear the backlog. The oldest
            (most urgent) items are kept; the newest overflow is dropped.
          </p>
        </div>
      )}

      {/* The list — preserves the API's stalest-first order. A flat, tight,
          scannable list (good to ~300 rows); item_type is on each row's badge
          so cross-client ordering by age is never broken by sectioning. */}
      <Card padded={false}>
        <ul className="divide-y divide-subtle">
          {items.map((item) => (
            <InboxRow
              key={`${item.workspace_id}:${item.id}`}
              item={item}
              onDecided={handleDecided}
            />
          ))}
        </ul>
      </Card>
    </div>
  );
}
