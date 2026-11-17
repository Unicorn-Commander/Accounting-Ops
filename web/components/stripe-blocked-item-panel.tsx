"use client";

// "Blocked item" panel on the Stripe connector settings page.
//
// Shows the item the Stripe sync is stopped on (amount, currency, error,
// failure count, since when) and the items a human resolved elsewhere, with
// who / when / why. Owner/admin humans get a Resolve form (pick a journal
// entry of this workspace OR give a not-books-relevant reason) and a Reopen
// form (required reason). Everyone else sees the state read-only.
//
// The backend decides everything (app/api/stripe_blocked_item.py). A 409
// "retry shortly" while a sync pass holds the lock is shown as a retry note,
// a 403 "requires a human session" as a plain sign-in note.

import Link from "next/link";
import { useState } from "react";
import { AlertTriangle, CheckCircle2, Loader2, RotateCcw } from "lucide-react";
import { StatusChip } from "@/components/primitives";
import { JournalEntryPicker, type PickedJournalEntry } from "@/components/journal-entry-picker";
import type { ApiFailure, FetchLike } from "@/lib/api-error";
import {
  MAX_REASON_CHARS,
  MIN_REASON_CHARS,
  fetchBlockedItemView,
  reasonLength,
  reasonProblem,
  reopenResolvedItem,
  resolveBlockedItem,
  type BlockedItemView,
  type BlockedStripeItem,
  type ResolvedStripeItem,
} from "@/lib/stripe-blocked-item";
import { formatStripeMinor } from "@/lib/stripe-review";

type Notice = { tone: "ok" | "retry" | "error"; message: string } | null;

function noticeFrom(failure: ApiFailure): Notice {
  return { tone: failure.kind === "retry" ? "retry" : "error", message: failure.message };
}

function NoticeLine({ notice }: { notice: Notice }) {
  if (!notice) return null;
  const cls =
    notice.tone === "ok" ? "text-credit" : notice.tone === "retry" ? "text-pending" : "text-debit";
  return (
    <p role={notice.tone === "error" ? "alert" : "status"} className={`mt-2 text-[12px] ${cls}`}>
      {notice.message}
    </p>
  );
}

function money(amount: number | null, currency: string | null): string {
  if (amount === null) return "Unknown amount";
  return formatStripeMinor(amount, currency);
}

function when(iso: string | null): string {
  if (!iso) return "unknown";
  const d = new Date(iso);
  return Number.isNaN(d.getTime()) ? iso : d.toISOString().replace("T", " ").slice(0, 16) + " UTC";
}

function ReasonField({
  id,
  label,
  value,
  onChange,
  min,
  max,
}: {
  id: string;
  label: string;
  value: string;
  onChange: (v: string) => void;
  min: number;
  max: number;
}) {
  const n = reasonLength(value);
  const problem = value ? reasonProblem(value, min, max) : null;
  return (
    <div className="flex flex-col gap-1">
      <label htmlFor={id} className="text-[11px] text-ink-tertiary">{label}</label>
      <textarea
        id={id}
        value={value}
        onChange={(e) => onChange(e.target.value)}
        maxLength={max}
        rows={3}
        className="rounded-md border border-subtle bg-surface p-2 text-[12px]"
      />
      <span className={`text-right text-[10px] ${problem ? "text-debit" : "text-ink-tertiary"}`} data-counter={id}>
        {n} / {max} (minimum {min})
      </span>
    </div>
  );
}

function ResolveForm({
  workspaceId,
  item,
  min,
  max,
  fetchImpl,
  onDone,
}: {
  workspaceId: string;
  item: BlockedStripeItem;
  min: number;
  max: number;
  fetchImpl: FetchLike;
  onDone: (message: string) => Promise<void>;
}) {
  const [mode, setMode] = useState<"journal_entry" | "not_books_relevant">("journal_entry");
  const [entry, setEntry] = useState<PickedJournalEntry | null>(null);
  const [reason, setReason] = useState("");
  const [busy, setBusy] = useState(false);
  const [notice, setNotice] = useState<Notice>(null);
  const valid = mode === "journal_entry" ? entry !== null : reasonProblem(reason, min, max) === null;

  async function submit() {
    setBusy(true);
    setNotice(null);
    const result = await resolveBlockedItem(
      fetchImpl,
      workspaceId,
      mode === "journal_entry"
        ? { mode, btId: item.bt_id, entityId: item.entity_id, journalEntryId: entry!.id }
        : { mode, btId: item.bt_id, entityId: item.entity_id, reason },
    );
    setBusy(false);
    if (!result.ok) {
      setNotice(noticeFrom(result));
      return;
    }
    await onDone(`Resolved ${item.bt_id}. Nothing was posted; the sync continues after it on its next pass.`);
  }

  const name = `resolve-mode-${item.bt_id}`;
  return (
    <form
      className="mt-3 flex flex-col gap-3 border-t border-subtle pt-3"
      aria-label={`Resolve ${item.bt_id}`}
      onSubmit={(e) => {
        e.preventDefault();
        if (valid && !busy) void submit();
      }}
    >
      <p className="text-[12px] font-semibold">Resolve as handled elsewhere</p>
      <div className="flex flex-wrap gap-4 text-[12px]">
        <label className="inline-flex items-center gap-1.5">
          <input type="radio" name={name} checked={mode === "journal_entry"} onChange={() => setMode("journal_entry")} />
          Booked by a manual journal entry
        </label>
        <label className="inline-flex items-center gap-1.5">
          <input type="radio" name={name} checked={mode === "not_books_relevant"} onChange={() => setMode("not_books_relevant")} />
          Not books-relevant
        </label>
      </div>
      {mode === "journal_entry" ? (
        <JournalEntryPicker
          workspaceId={workspaceId}
          entityId={item.entity_id}
          value={entry}
          onChange={setEntry}
          fetchImpl={fetchImpl}
        />
      ) : (
        <ReasonField
          id={`resolve-reason-${item.bt_id}`}
          label="Why this item does not belong in the books"
          value={reason}
          onChange={setReason}
          min={min}
          max={max}
        />
      )}
      <div>
        <button
          type="submit"
          disabled={!valid || busy}
          className="inline-flex h-8 items-center gap-1.5 rounded-md bg-brand-500 px-3 text-xs font-medium text-white hover:bg-brand-600 disabled:opacity-50"
        >
          {busy ? <Loader2 size={12} className="animate-spin" /> : <CheckCircle2 size={12} />}
          Resolve
        </button>
      </div>
      <NoticeLine notice={notice} />
    </form>
  );
}

function ReopenForm({
  workspaceId,
  item,
  min,
  max,
  fetchImpl,
  onDone,
}: {
  workspaceId: string;
  item: ResolvedStripeItem;
  min: number;
  max: number;
  fetchImpl: FetchLike;
  onDone: (message: string) => Promise<void>;
}) {
  const [open, setOpen] = useState(false);
  const [reason, setReason] = useState("");
  const [busy, setBusy] = useState(false);
  const [notice, setNotice] = useState<Notice>(null);
  const valid = reasonProblem(reason, min, max) === null;

  if (!open) {
    return (
      <button
        type="button"
        onClick={() => setOpen(true)}
        className="inline-flex h-7 items-center gap-1 rounded-md border border-subtle bg-surface px-2.5 text-[11px] text-ink-secondary hover:bg-elevated"
      >
        <RotateCcw size={11} /> Reopen
      </button>
    );
  }

  async function submit() {
    setBusy(true);
    setNotice(null);
    const result = await reopenResolvedItem(fetchImpl, workspaceId, item.bt_id, item.entity_id, reason);
    setBusy(false);
    if (!result.ok) {
      setNotice(noticeFrom(result));
      return;
    }
    const warning = typeof result.data.warning === "string" ? ` ${result.data.warning}` : "";
    await onDone(`Reopened ${item.bt_id}. The next sync pass retries it.${warning}`);
  }

  return (
    <form
      className="mt-2 flex flex-col gap-2"
      aria-label={`Reopen ${item.bt_id}`}
      onSubmit={(e) => {
        e.preventDefault();
        if (valid && !busy) void submit();
      }}
    >
      <ReasonField
        id={`reopen-reason-${item.bt_id}`}
        label="Why reopen this resolution (required)"
        value={reason}
        onChange={setReason}
        min={min}
        max={max}
      />
      <div className="flex gap-2">
        <button
          type="submit"
          disabled={!valid || busy}
          className="inline-flex h-8 items-center gap-1.5 rounded-md bg-brand-500 px-3 text-xs font-medium text-white hover:bg-brand-600 disabled:opacity-50"
        >
          {busy ? <Loader2 size={12} className="animate-spin" /> : <RotateCcw size={12} />}
          Reopen
        </button>
        <button type="button" onClick={() => setOpen(false)} className="h-8 px-2 text-xs text-ink-tertiary hover:text-ink-secondary">
          Cancel
        </button>
      </div>
      <NoticeLine notice={notice} />
    </form>
  );
}

export function StripeBlockedItemPanel({
  workspaceId,
  initialView,
  canManage,
  fetchImpl,
}: {
  workspaceId: string;
  initialView: BlockedItemView | null;
  canManage: boolean;
  fetchImpl?: FetchLike;
}) {
  const doFetch: FetchLike = fetchImpl ?? ((input, init) => fetch(input, init));
  const [view, setView] = useState<BlockedItemView | null>(initialView);
  const [notice, setNotice] = useState<Notice>(null);
  const min = view?.min_reason_chars ?? MIN_REASON_CHARS;
  const max = view?.max_reason_chars ?? MAX_REASON_CHARS;

  async function refresh(message: string) {
    const next = await fetchBlockedItemView(doFetch, workspaceId);
    if (next) setView(next);
    setNotice({ tone: "ok", message: next ? message : `${message} Refresh the page to see the new state.` });
  }

  return (
    <section aria-labelledby="stripe-blocked-item-heading" className="flex flex-col gap-4">
      <div className="flex items-center gap-2">
        <h2 id="stripe-blocked-item-heading" className="text-sm font-semibold">Blocked item</h2>
        {view && view.blocked.length > 0 && <StatusChip tone="blocked">Sync stopped</StatusChip>}
      </div>

      {!view ? (
        <p role="alert" className="text-[12px] text-ink-secondary">
          The blocked-item state is unavailable right now. Refresh the page to try again.
        </p>
      ) : view.blocked.length === 0 ? (
        <p className="text-[12px] text-ink-secondary">Nothing is blocked. The Stripe sync is not stopped on any item.</p>
      ) : (
        view.blocked.map((item) => (
          <div key={`${item.entity_id}:${item.bt_id}`} className="rounded-md border border-blocked/40 bg-blocked/[0.05] p-3 text-[12px]">
            <div className="flex items-start gap-2">
              <AlertTriangle size={14} className="mt-0.5 shrink-0 text-blocked" />
              <dl className="grid flex-1 grid-cols-[max-content_1fr] gap-x-4 gap-y-1 text-ink-secondary">
                <dt className="text-ink-tertiary">Balance transaction</dt>
                <dd className="break-all font-mono text-ink-primary">{item.bt_id}</dd>
                <dt className="text-ink-tertiary">Amount</dt>
                <dd className="font-mono">{money(item.amount, item.currency)} <span className="text-ink-tertiary">{(item.currency ?? "").toUpperCase()}</span></dd>
                <dt className="text-ink-tertiary">Error</dt>
                <dd className="break-words font-mono text-debit">{item.error ?? "No error text recorded"}</dd>
                <dt className="text-ink-tertiary">Failures</dt>
                <dd className="font-mono">{item.failure_count}</dd>
                <dt className="text-ink-tertiary">Since</dt>
                <dd className="font-mono">{when(item.since)}</dd>
                {item.entity_display_name && (<><dt className="text-ink-tertiary">Entity</dt><dd>{item.entity_display_name}</dd></>)}
              </dl>
            </div>
            {item.approval_id && (
              <p className="mt-2 text-ink-tertiary">
                Its review row is in{" "}
                <Link href="/approvals?tab=rows" className="text-brand-400 underline-offset-2 hover:underline">Approvals</Link>
                {item.approval_status ? ` (${item.approval_status})` : ""}. Fix the config and approve it there, or resolve it here if it was handled another way.
              </p>
            )}
            {canManage ? (
              <ResolveForm workspaceId={workspaceId} item={item} min={min} max={max} fetchImpl={doFetch} onDone={refresh} />
            ) : (
              <p className="mt-3 text-[11px] text-ink-tertiary">Only a workspace owner or admin can resolve a blocked item.</p>
            )}
          </div>
        ))
      )}

      <NoticeLine notice={notice} />

      {view && (
        <div className="flex flex-col gap-2">
          <h3 className="text-[12px] font-semibold">Resolved elsewhere</h3>
          {view.resolved.length === 0 ? (
            <p className="text-[12px] text-ink-tertiary">No items have been resolved by hand.</p>
          ) : (
            <ul className="flex flex-col gap-2">
              {view.resolved.map((r) => (
                <li key={`${r.entity_id}:${r.bt_id}`} className="rounded-md border border-subtle p-3 text-[12px]" data-resolved={r.bt_id}>
                  <div className="flex flex-wrap items-center gap-2">
                    <span className="break-all font-mono text-ink-primary">{r.bt_id}</span>
                    <span className="font-mono text-ink-secondary">{money(r.amount, r.currency)}</span>
                    <StatusChip tone="ink">{r.kind === "journal_entry" ? "Manual journal entry" : "Not books-relevant"}</StatusChip>
                  </div>
                  <p className="mt-1 text-ink-secondary">
                    By <span className="font-mono">{r.resolved_by_uc_uid ?? "unknown"}</span> at {when(r.resolved_at)}
                  </p>
                  <p className="mt-1 break-words text-ink-secondary">
                    {r.kind === "journal_entry"
                      ? <>Journal entry <span className="font-mono">{r.journal_entry_id ?? "unknown"}</span></>
                      : <>Reason: {r.not_books_relevant_reason ?? "none recorded"}</>}
                  </p>
                  {r.audit_id && <p className="mt-1 font-mono text-[10px] text-ink-tertiary">Audit {r.audit_id}</p>}
                  {canManage && (
                    <div className="mt-2">
                      <ReopenForm workspaceId={workspaceId} item={r} min={min} max={max} fetchImpl={doFetch} onDone={refresh} />
                    </div>
                  )}
                </li>
              ))}
            </ul>
          )}
        </div>
      )}
    </section>
  );
}
