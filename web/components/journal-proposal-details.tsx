import React from "react";
import type { ApprovalPayload } from "@/lib/approvals";

export function canReviewJournalProposal(payload: ApprovalPayload): boolean {
  const r = payload.request;
  return payload.version === 1 && !!r &&
    [payload.workspace_id, payload.entity_id, payload.debit_account, payload.credit_account,
      r.source, r.source_external_id, r.effective_date, r.description, r.category,
      r.category_status, r.external_account_id].every(v => typeof v === "string" && v.trim().length > 0) &&
    Number.isSafeInteger(r.amount_cents) && r.amount_cents !== 0 &&
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
