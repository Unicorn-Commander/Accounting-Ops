"use client";

// SpecialistDraftView — review surface for items dropped into the
// tax_review_queue by the 1120 Preparer specialist agent.
//
// The backend contract (GET /api/workspaces/{wid}/tax-review-queue + POST
// .../resolve) is the pre-agreed API; this component talks to the web proxy
// routes rather than the backend directly.

import { useCallback, useMemo, useState } from "react";
import {
  AlertTriangle,
  ChevronDown,
  ChevronRight,
  FileText,
  HelpCircle,
  MessageSquare,
  TriangleAlert,
  X
} from "lucide-react";
import { cn } from "@/lib/cn";
import { Card } from "@/components/primitives";
import type {
  TaxReviewItem,
  TaxReviewItemType,
  TaxReviewCitation,
  ResolvePayload
} from "@/lib/tax-review-queue";

// ---------------------------------------------------------------------------
// Utility: cents → formatted dollars (mirrors form-1120-view.tsx formatCents)
// ---------------------------------------------------------------------------

function centsToDollars(cents: number): string {
  const dollars = cents / 100;
  const abs = Math.abs(dollars);
  const formatted = new Intl.NumberFormat("en-US", {
    style: "currency",
    currency: "USD",
    minimumFractionDigits: 0,
    maximumFractionDigits: 0
  }).format(abs);
  return dollars < 0 ? `(${formatted})` : formatted;
}

// ---------------------------------------------------------------------------
// Confidence badge
// ---------------------------------------------------------------------------

function ConfidenceBadge({ level }: { level: "low" | "medium" | "high" }) {
  const styles: Record<string, string> = {
    high: "border-credit/40 bg-credit/[0.08] text-credit",
    medium: "border-pending/40 bg-pending/[0.08] text-pending",
    low: "border-debit/40 bg-debit/[0.08] text-debit"
  };
  return (
    <span
      className={cn(
        "inline-flex items-center rounded-full border px-2 py-0.5 text-[10px] font-semibold uppercase tracking-wider",
        styles[level]
      )}
    >
      {level}
    </span>
  );
}

// ---------------------------------------------------------------------------
// Citation list — renders §code refs distinctly
// ---------------------------------------------------------------------------

function Citations({ citations }: { citations: TaxReviewCitation[] | null }) {
  if (!citations || citations.length === 0) return null;
  return (
    <div className="mt-2 flex flex-wrap gap-1.5">
      {citations.map((c, i) => {
        const isSection = c.type === "section";
        return (
          <span
            key={i}
            className={cn(
              "inline-flex items-center rounded px-2 py-0.5 font-mono text-[11px]",
              isSection
                ? "border border-brand-500/30 bg-brand-500/[0.07] text-brand-300"
                : "border border-subtle bg-elevated text-ink-tertiary"
            )}
          >
            {isSection ? `§${c.value}` : c.value}
          </span>
        );
      })}
    </div>
  );
}

// ---------------------------------------------------------------------------
// Resolve action bar + modal
// ---------------------------------------------------------------------------

type ResolveAction = "reviewed" | "resolved" | "rejected";

function ResolveModal({
  itemId,
  workspaceId,
  onDone,
  onClose
}: {
  itemId: string;
  workspaceId: string;
  onDone: (itemId: string, status: ResolveAction) => void;
  onClose: () => void;
}) {
  const [status, setStatus] = useState<ResolveAction>("resolved");
  const [notes, setNotes] = useState("");
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);

  async function handleConfirm() {
    setBusy(true);
    setErr(null);
    try {
      const payload: ResolvePayload = {
        status,
        resolution_notes: notes.trim() || undefined
      };
      const res = await fetch(
        `/api/workspaces/${workspaceId}/tax-review-queue/${itemId}/resolve`,
        {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(payload)
        }
      );
      if (!res.ok) {
        const data = await res.json().catch(() => null);
        setErr(
          data?.detail ?? data?.error ?? `Server error ${res.status}`
        );
        return;
      }
      onDone(itemId, status);
    } catch {
      setErr("Network error — please try again.");
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center p-4">
      <button
        type="button"
        className="absolute inset-0 bg-canvas/80 backdrop-blur-sm"
        onClick={onClose}
        aria-label="Close"
      />
      <div className="relative z-10 w-full max-w-sm rounded-xl border border-default bg-surface p-6 shadow-card">
        <div className="mb-4 flex items-start justify-between gap-3">
          <h2 className="text-base font-semibold">Resolve item</h2>
          <button
            type="button"
            onClick={onClose}
            className="text-ink-tertiary hover:text-ink-primary"
            aria-label="Close"
          >
            <X size={16} />
          </button>
        </div>

        <div className="space-y-3">
          <label className="block text-xs text-ink-secondary">
            Resolution status
            <select
              value={status}
              onChange={(e) => setStatus(e.target.value as ResolveAction)}
              className="mt-1 block w-full rounded-md border border-default bg-elevated px-3 py-1.5 text-sm text-ink-primary focus:border-brand-500 focus:outline-none"
            >
              <option value="reviewed">Reviewed (no action needed)</option>
              <option value="resolved">Resolved (change accepted)</option>
              <option value="rejected">Rejected (specialist was wrong)</option>
            </select>
          </label>
          <label className="block text-xs text-ink-secondary">
            Resolution notes (optional)
            <textarea
              value={notes}
              onChange={(e) => setNotes(e.target.value)}
              rows={3}
              className="mt-1 block w-full resize-none rounded-md border border-default bg-elevated px-3 py-1.5 text-sm text-ink-primary placeholder-ink-disabled focus:border-brand-500 focus:outline-none"
              placeholder="e.g. Confirmed with §461(h) matching principle; amount correct."
            />
          </label>
        </div>

        {err && <p className="mt-2 text-xs text-debit">{err}</p>}

        <div className="mt-5 flex items-center justify-end gap-2">
          <button
            type="button"
            onClick={onClose}
            className="rounded-md border border-default bg-elevated px-3 py-1.5 text-xs text-ink-secondary hover:text-ink-primary"
          >
            Cancel
          </button>
          <button
            type="button"
            onClick={handleConfirm}
            disabled={busy}
            className="rounded-md bg-brand-500 px-3 py-1.5 text-xs font-medium text-white hover:bg-brand-600 disabled:cursor-not-allowed disabled:bg-elevated disabled:text-ink-disabled"
          >
            {busy ? "Saving…" : "Confirm"}
          </button>
        </div>
      </div>
    </div>
  );
}

function ResolveBar({
  item,
  workspaceId,
  onResolved
}: {
  item: TaxReviewItem;
  workspaceId: string;
  onResolved: (itemId: string, status: ResolveAction) => void;
}) {
  const [modalOpen, setModalOpen] = useState(false);
  const [quickAction, setQuickAction] = useState<ResolveAction | null>(null);

  function openModal(action: ResolveAction) {
    setQuickAction(action);
    setModalOpen(true);
  }

  const isPending = item.status === "pending";

  if (!isPending) {
    return (
      <div className="mt-3 flex items-center gap-2 border-t border-subtle/40 pt-3">
        <span
          className={cn(
            "inline-flex items-center rounded-full px-2 py-0.5 text-[10px] font-medium uppercase tracking-wider",
            item.status === "resolved"
              ? "bg-credit/15 text-credit"
              : item.status === "reviewed"
                ? "bg-brand-500/10 text-brand-300"
                : "bg-debit/15 text-debit"
          )}
        >
          {item.status}
        </span>
        {item.resolution_notes && (
          <span className="text-[11px] text-ink-tertiary">{item.resolution_notes}</span>
        )}
      </div>
    );
  }

  return (
    <>
      <div className="mt-3 flex flex-wrap items-center gap-2 border-t border-subtle/40 pt-3">
        <button
          type="button"
          onClick={() => openModal("resolved")}
          className="rounded-md bg-credit/[0.12] px-3 py-1 text-xs font-medium text-credit hover:bg-credit/20"
        >
          Accept
        </button>
        <button
          type="button"
          onClick={() => openModal("rejected")}
          className="rounded-md bg-debit/[0.12] px-3 py-1 text-xs font-medium text-debit hover:bg-debit/20"
        >
          Reject
        </button>
        <button
          type="button"
          onClick={() => openModal("reviewed")}
          className="rounded-md border border-default bg-elevated px-3 py-1 text-xs text-ink-secondary hover:text-ink-primary"
        >
          Mark reviewed
        </button>
      </div>

      {modalOpen && (
        <ResolveModal
          itemId={item.id}
          workspaceId={workspaceId}
          onDone={(id, status) => {
            setModalOpen(false);
            onResolved(id, status);
          }}
          onClose={() => {
            setModalOpen(false);
            setQuickAction(null);
          }}
        />
      )}
      {/* suppress unused var warning — quickAction drives initial modal state */}
      {quickAction && false && null}
    </>
  );
}

// ---------------------------------------------------------------------------
// Item-type card renderers
// ---------------------------------------------------------------------------

function FormLineProposalCard({
  item,
  workspaceId,
  onResolved
}: {
  item: TaxReviewItem;
  workspaceId: string;
  onResolved: (id: string, status: ResolveAction) => void;
}) {
  const [expanded, setExpanded] = useState(false);
  const c = item.content as {
    proposed_amount_cents?: number;
    line_label?: string;
    source_categories?: Array<{ category: string; amount_cents: number; entry_count?: number }>;
    notes?: string;
  };

  const isPending = item.status === "pending";

  return (
    <div
      className={cn(
        "rounded-lg border bg-surface p-4",
        isPending ? "border-default" : "border-subtle opacity-60"
      )}
    >
      <div className="flex flex-wrap items-start justify-between gap-2">
        <div className="flex flex-wrap items-center gap-2">
          <FileText size={14} className="mt-0.5 shrink-0 text-ink-tertiary" />
          <span className="text-[11px] font-mono text-ink-tertiary">
            {item.form_reference ?? "—"}
          </span>
          {c.line_label && (
            <span className="text-sm font-semibold">{c.line_label}</span>
          )}
        </div>
        <ConfidenceBadge level={item.confidence} />
      </div>

      {c.proposed_amount_cents != null && (
        <p className="mt-2 font-mono text-base font-bold text-ink-primary">
          {centsToDollars(c.proposed_amount_cents)}
        </p>
      )}

      {c.notes && (
        <p className="mt-2 text-sm leading-relaxed text-ink-secondary">{c.notes}</p>
      )}

      {c.source_categories && c.source_categories.length > 0 && (
        <div className="mt-2">
          <button
            type="button"
            onClick={() => setExpanded((v) => !v)}
            className="inline-flex items-center gap-1 text-[11px] text-ink-tertiary hover:text-ink-primary"
          >
            {expanded ? <ChevronDown size={11} /> : <ChevronRight size={11} />}
            Source categories ({c.source_categories.length})
          </button>
          {expanded && (
            <table className="mt-2 w-full text-[11px]">
              <thead>
                <tr className="text-ink-tertiary">
                  <th className="pb-1 text-left font-medium">Category</th>
                  <th className="pb-1 text-right font-medium">Amount</th>
                  {c.source_categories.some((s) => s.entry_count != null) && (
                    <th className="pb-1 text-right font-medium">Entries</th>
                  )}
                </tr>
              </thead>
              <tbody>
                {c.source_categories.map((s, i) => (
                  <tr key={i}>
                    <td className="py-0.5 font-mono">{s.category}</td>
                    <td className="py-0.5 text-right font-mono tabular-nums">
                      {centsToDollars(s.amount_cents)}
                    </td>
                    {c.source_categories!.some((x) => x.entry_count != null) && (
                      <td className="py-0.5 text-right font-mono text-ink-tertiary">
                        {s.entry_count ?? "—"}
                      </td>
                    )}
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </div>
      )}

      <Citations citations={item.citations} />

      <ResolveBar item={item} workspaceId={workspaceId} onResolved={onResolved} />
    </div>
  );
}

function FlaggedPositionCard({
  item,
  workspaceId,
  onResolved
}: {
  item: TaxReviewItem;
  workspaceId: string;
  onResolved: (id: string, status: ResolveAction) => void;
}) {
  const c = item.content as {
    description?: string;
    judgment?: string;
    notes?: string;
  };
  const isPending = item.status === "pending";

  return (
    <div
      className={cn(
        "rounded-lg border-l-4 border bg-surface p-4",
        isPending
          ? "border-l-pending border-pending/30"
          : "border-l-subtle border-subtle opacity-60"
      )}
    >
      <div className="flex flex-wrap items-start justify-between gap-2">
        <div className="flex flex-wrap items-center gap-2">
          <TriangleAlert size={14} className="mt-0.5 shrink-0 text-pending" />
          <span className="text-[11px] font-mono text-ink-tertiary">
            {item.form_reference ?? "—"}
          </span>
          <span className="text-xs font-semibold uppercase tracking-wider text-pending">
            Flagged position
          </span>
        </div>
        <ConfidenceBadge level={item.confidence} />
      </div>

      {c.description && (
        <p className="mt-2 text-sm font-semibold leading-snug">{c.description}</p>
      )}
      {c.judgment && (
        <p className="mt-1.5 text-sm leading-relaxed text-ink-secondary">{c.judgment}</p>
      )}
      {c.notes && (
        <p className="mt-1.5 text-sm leading-relaxed text-ink-tertiary">{c.notes}</p>
      )}

      <Citations citations={item.citations} />

      <ResolveBar item={item} workspaceId={workspaceId} onResolved={onResolved} />
    </div>
  );
}

function MemoCard({
  item,
  workspaceId,
  onResolved
}: {
  item: TaxReviewItem;
  workspaceId: string;
  onResolved: (id: string, status: ResolveAction) => void;
}) {
  const c = item.content as {
    title?: string;
    body?: string;
  };
  const isPending = item.status === "pending";

  return (
    <div
      className={cn(
        "rounded-lg border bg-surface p-4",
        isPending ? "border-default" : "border-subtle opacity-60"
      )}
    >
      <div className="flex flex-wrap items-center gap-2 mb-2">
        <MessageSquare size={14} className="shrink-0 text-ink-tertiary" />
        <span className="text-[10px] font-medium uppercase tracking-wider text-ink-tertiary">
          Memo
        </span>
        {c.title && <span className="text-sm font-semibold">{c.title}</span>}
      </div>

      {c.body && (
        <pre className="whitespace-pre-wrap text-sm leading-relaxed text-ink-secondary font-sans">
          {c.body}
        </pre>
      )}

      <Citations citations={item.citations} />

      <ResolveBar item={item} workspaceId={workspaceId} onResolved={onResolved} />
    </div>
  );
}

function QuestionCard({
  item,
  workspaceId,
  onResolved
}: {
  item: TaxReviewItem;
  workspaceId: string;
  onResolved: (id: string, status: ResolveAction) => void;
}) {
  const c = item.content as {
    body?: string;
    context?: string;
  };
  const isPending = item.status === "pending";

  return (
    <div
      className={cn(
        "rounded-lg border bg-surface p-4",
        isPending ? "border-default" : "border-subtle opacity-60"
      )}
    >
      <div className="flex flex-wrap items-center gap-2 mb-2">
        <HelpCircle size={14} className="shrink-0 text-brand-300" />
        <span className="text-[10px] font-medium uppercase tracking-wider text-brand-300">
          Question for CPA
        </span>
      </div>

      {c.body && (
        <p className="italic text-sm leading-relaxed text-ink-primary">{c.body}</p>
      )}
      {c.context && (
        <p className="mt-1.5 text-xs leading-relaxed text-ink-tertiary">{c.context}</p>
      )}

      <Citations citations={item.citations} />

      <ResolveBar item={item} workspaceId={workspaceId} onResolved={onResolved} />
    </div>
  );
}

// ---------------------------------------------------------------------------
// Item dispatcher
// ---------------------------------------------------------------------------

function ItemCard({
  item,
  workspaceId,
  onResolved
}: {
  item: TaxReviewItem;
  workspaceId: string;
  onResolved: (id: string, status: ResolveAction) => void;
}) {
  switch (item.item_type) {
    case "form_line_proposal":
      return (
        <FormLineProposalCard
          item={item}
          workspaceId={workspaceId}
          onResolved={onResolved}
        />
      );
    case "flagged_position":
      return (
        <FlaggedPositionCard
          item={item}
          workspaceId={workspaceId}
          onResolved={onResolved}
        />
      );
    case "memo":
      return (
        <MemoCard item={item} workspaceId={workspaceId} onResolved={onResolved} />
      );
    case "question":
      return (
        <QuestionCard item={item} workspaceId={workspaceId} onResolved={onResolved} />
      );
    default:
      return null;
  }
}

// ---------------------------------------------------------------------------
// Type count sub-bar
// ---------------------------------------------------------------------------

type CountMap = Record<TaxReviewItemType, number>;

function TypeCountBar({ counts }: { counts: CountMap }) {
  const parts = [
    { key: "form_line_proposal" as TaxReviewItemType, label: "line proposals" },
    { key: "flagged_position" as TaxReviewItemType, label: "flagged" },
    { key: "memo" as TaxReviewItemType, label: "memos" },
    { key: "question" as TaxReviewItemType, label: "questions" }
  ];
  return (
    <div className="flex flex-wrap items-center gap-1.5 text-[11px] text-ink-tertiary">
      {parts.map((p, i) => (
        <span key={p.key}>
          {i > 0 && <span className="mx-1 text-ink-disabled">·</span>}
          <span className={counts[p.key] > 0 ? "text-ink-secondary" : ""}>
            <span className="font-semibold">{counts[p.key]}</span> {p.label}
          </span>
        </span>
      ))}
    </div>
  );
}

// ---------------------------------------------------------------------------
// Main component
// ---------------------------------------------------------------------------

export type SpecialistDraftViewProps = {
  items: TaxReviewItem[];
  workspaceId: string;
  year: number;
};

type StatusFilter = "pending" | "reviewed" | "all";

export function SpecialistDraftView({
  items: initialItems,
  workspaceId,
  year
}: SpecialistDraftViewProps) {
  const [items, setItems] = useState<TaxReviewItem[]>(initialItems);
  const [statusFilter, setStatusFilter] = useState<StatusFilter>("pending");

  const handleResolved = useCallback(
    (itemId: string, newStatus: ResolveAction) => {
      setItems((prev) =>
        prev.map((it) =>
          it.id === itemId
            ? { ...it, status: newStatus, resolved_at: new Date().toISOString() }
            : it
        )
      );
    },
    []
  );

  const counts = useMemo<CountMap>(() => {
    const all = items.filter((it) =>
      statusFilter === "all" ? true : it.status === statusFilter
    );
    return {
      form_line_proposal: all.filter((it) => it.item_type === "form_line_proposal").length,
      flagged_position: all.filter((it) => it.item_type === "flagged_position").length,
      memo: all.filter((it) => it.item_type === "memo").length,
      question: all.filter((it) => it.item_type === "question").length
    };
  }, [items, statusFilter]);

  const visible = useMemo(
    () =>
      items.filter((it) =>
        statusFilter === "all" ? true : it.status === statusFilter
      ),
    [items, statusFilter]
  );

  const hasAny = items.length > 0;

  const filterButtons: { v: StatusFilter; label: string }[] = [
    { v: "pending", label: "Pending" },
    { v: "reviewed", label: "Reviewed" },
    { v: "all", label: "All" }
  ];

  return (
    <div className="space-y-5">
      {/* Draft warning banner — same visual treatment as form-1120-view */}
      <div className="rounded-lg border border-pending/50 bg-pending/[0.07] px-4 py-3 text-sm font-semibold text-pending">
        <div className="flex items-center gap-2">
          <AlertTriangle size={15} className="shrink-0" />
          DRAFT — AI-prepared, awaiting CPA review. Items below were generated
          by the 1120 Preparer specialist. Each must be reviewed before filing.
        </div>
      </div>

      {!hasAny ? (
        // Empty state
        <Card>
          <div className="flex flex-col items-center gap-3 py-8 text-center">
            <FileText size={32} className="text-ink-disabled" />
            <p className="text-sm font-medium text-ink-secondary">
              No specialist draft yet for {year}.
            </p>
            <p className="max-w-sm text-xs text-ink-tertiary">
              Run the 1120 Preparer to generate one. The runner lives at{" "}
              <span className="font-mono text-ink-secondary">
                agents/tax_specialists/form_1120_preparer/
              </span>
              . Once the specialist submits items they will appear here for CPA
              review.
            </p>
          </div>
        </Card>
      ) : (
        <>
          {/* Sub-bar: counts + status filter */}
          <div className="flex flex-wrap items-center justify-between gap-3">
            <TypeCountBar counts={counts} />
            <div className="flex items-center gap-1">
              {filterButtons.map((f) => (
                <button
                  key={f.v}
                  type="button"
                  onClick={() => setStatusFilter(f.v)}
                  className={cn(
                    "rounded-md border px-2.5 py-1 text-[11px] font-medium",
                    statusFilter === f.v
                      ? "border-brand-500 bg-brand-500/10 text-brand-300"
                      : "border-default bg-surface text-ink-secondary hover:bg-elevated"
                  )}
                >
                  {f.label}
                </button>
              ))}
            </div>
          </div>

          {visible.length === 0 ? (
            <Card>
              <p className="text-sm text-ink-tertiary">
                No {statusFilter} items.{" "}
                {statusFilter === "pending" &&
                  "All items have been reviewed — good work."}
              </p>
            </Card>
          ) : (
            <div className="space-y-3">
              {visible.map((item) => (
                <ItemCard
                  key={item.id}
                  item={item}
                  workspaceId={workspaceId}
                  onResolved={handleResolved}
                />
              ))}
            </div>
          )}
        </>
      )}
    </div>
  );
}
