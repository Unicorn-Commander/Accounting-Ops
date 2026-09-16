import React from "react";
import type { ApprovalPayload } from "@/lib/approvals";

export function canReviewJournalProposal(payload: ApprovalPayload): boolean {
  const r = payload.request;
  if (!r || payload.version !== 1) return false;
  const envelopeKeys = new Set(["version", "workspace_id", "entity_id", "request", "debit_account", "credit_account"]);
  const requestKeys = new Set(["source", "source_external_id", "effective_date", "amount_cents", "description", "category", "category_status", "payee", "memo", "sign_matched", "external_account_id", "receipt_link_id", "reasoning_trace", "model_version"]);
  const date = new Date(r.effective_date);
  return Object.keys(payload).every(k => envelopeKeys.has(k)) &&
    Object.keys(r).every(k => requestKeys.has(k)) &&
    [payload.workspace_id, payload.entity_id, payload.debit_account, payload.credit_account,
      r.source, r.source_external_id, r.effective_date, r.description, r.category,
      r.category_status, r.external_account_id].every(v => typeof v === "string" && v.trim().length > 0) &&
    [r.payee, r.memo, r.receipt_link_id, r.reasoning_trace, r.model_version]
      .every(v => v == null || typeof v === "string") &&
    /^\d{4}-\d{2}-\d{2}$/.test(r.effective_date) && !Number.isNaN(date.getTime()) &&
    date.toISOString().slice(0, 10) === r.effective_date &&
    ["auto", "suggest", "unknown"].includes(r.category_status) &&
    r.source.length <= 64 && r.source_external_id.length <= 256 &&
    r.description.length <= 512 && r.category.length <= 64 &&
    (r.payee?.length ?? 0) <= 256 && (r.model_version?.length ?? 0) <= 128 &&
    Number.isSafeInteger(r.amount_cents) && r.amount_cents !== 0 && Math.abs(r.amount_cents) <= 100_000_000_000 &&
    typeof r.sign_matched === "boolean";
}

export function JournalProposalDetails({ payload }: { payload: ApprovalPayload }) {
  if (!canReviewJournalProposal(payload)) {
    return <p role="alert" className="mt-3 text-sm text-ink-secondary">
      This journal request is incomplete or unsupported. Its details must be corrected before approval.
    </p>;
  }
  const r = payload.request!;
  const amount = new Intl.NumberFormat("en-US", { style: "currency", currency: "USD" })
    .format(Math.abs(r.amount_cents) / 100);
  const rows = [
    ["Effective date", r.effective_date],
    ["Description", r.description],
    ["Payee", r.payee || "Not supplied"],
    ["Category", r.category],
    ["Debit", `${payload.debit_account} · ${amount}`],
    ["Credit", `${payload.credit_account} · ${amount}`],
    ["Cash movement", `${r.amount_cents < 0 ? "Outflow" : "Inflow"} · ${amount}`],
    ["Cash account", r.external_account_id!],
    ["Source", `${r.source} · ${r.source_external_id}`],
    ["Receipt link", r.receipt_link_id || "None"],
    ["Memo", r.memo || "None"],
    ["Category status", r.category_status],
    ["Direction check", r.sign_matched ? "Requested; verified again when applied" : "Not confirmed"],
  ];
  return <section aria-label="Journal request details" className="mt-3 space-y-3">
    <p className="text-sm text-ink-secondary">Approving posts these exact journal details. Changed details require a new request.</p>
    <dl className="grid gap-x-4 gap-y-2 text-sm sm:grid-cols-[9rem_minmax(0,1fr)]">
      {rows.map(([label, value]) => <div key={label} className="contents">
        <dt className="text-ink-tertiary">{label}</dt>
        <dd className="min-w-0 break-words text-ink-secondary">{value}</dd>
      </div>)}
    </dl>
    {r.reasoning_trace && <p className="whitespace-pre-wrap break-words text-sm text-ink-secondary">{r.reasoning_trace}</p>}
  </section>;
}
