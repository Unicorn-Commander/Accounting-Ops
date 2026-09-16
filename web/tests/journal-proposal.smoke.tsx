import React from "react";
import assert from "node:assert/strict";
import { renderToStaticMarkup } from "react-dom/server";
import { JournalProposalDetails, canReviewJournalProposal } from "../components/journal-proposal-details";
import type { ApprovalPayload } from "../lib/approvals";

const payload: ApprovalPayload = {
  version: 1, workspace_id: "demo-workspace", entity_id: "demo-entity",
  debit_account: "expense:payroll", credit_account: "asset:cash",
  request: {
    source: "demo-seed", source_external_id: "rivergreen:payroll:one",
    effective_date: "2026-08-31", amount_cents: -2050000,
    description: "Synthetic <payroll>", category: "Payroll · Wages", category_status: "auto",
    external_account_id: "demo-cash", payee: "Fictional staff", memo: "August batch",
    receipt_link_id: null, sign_matched: true,
  },
};
const html = renderToStaticMarkup(<JournalProposalDetails payload={payload} />);
for (const text of ["2026-08-31", "$20,500.00", "expense:payroll", "asset:cash", "Outflow",
  "rivergreen:payroll:one", "demo-cash", "Fictional staff", "August batch", "Synthetic &lt;payroll&gt;"])
  assert.ok(html.includes(text), `Missing approval detail: ${text}`);
assert.ok(!html.includes("<payroll>"), "Description must be escaped");
assert.equal(canReviewJournalProposal(payload), true);
for (const invalid of [
  { ...payload, version: 2 },
  { ...payload, debit_account: undefined },
  { ...payload, request: { ...payload.request!, amount_cents: NaN } },
  { ...payload, request: { ...payload.request!, external_account_id: null } },
]) {
  assert.equal(canReviewJournalProposal(invalid), false);
  assert.ok(renderToStaticMarkup(<JournalProposalDetails payload={invalid} />).includes('role="alert"'));
}
console.log("Journal proposal review details: passed");
