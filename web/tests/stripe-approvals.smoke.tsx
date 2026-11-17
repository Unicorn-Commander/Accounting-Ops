import React from "react";
import assert from "node:assert/strict";
import { renderToStaticMarkup } from "react-dom/server";
import { StripeApprovalDetails } from "../components/stripe-approval-details";
import { decideApproval } from "../lib/approval-decide";
import { HUMAN_SESSION_MESSAGE, RETRY_MESSAGE, type FetchLike } from "../lib/api-error";
import {
  STRIPE_POST_BALANCE_TRANSACTION,
  formatStripeMinor,
  isStripeSubject,
  stripeReviewKind,
  stripeReviewView,
} from "../lib/stripe-review";

// Fixtures mirror app/connectors/stripe/mapper.py _enqueue_for_review payloads.
const base = {
  action: STRIPE_POST_BALANCE_TRANSACTION,
  stripe_balance_txn_id: "txn_fixture_0001",
  stripe_type: "charge",
  amount_cents: 12345,
  debit_account: "1210·Stripe Receivable",
  credit_account: "4200·Sales",
  effective_date: "2026-11-10",
  payee: "Stripe charge",
  description: "Stripe charge",
  stripe_txn: { id: "txn_fixture_0001", type: "charge", amount: 12345, currency: "usd", created: 1794268800 },
};
const reasons = {
  first_run: "First-30-rows review guard: stripe txn #1 for this entity needs human eyes before auto-posting trust is established.",
  confirm: "Materiality tier M: amount above the auto-post threshold",
  cpa: "Stripe type 'adjustment' requires CPA review. Adjustment / Connect platform fee / transfer, no default mapping.",
  blocked: "(blocked) Stripe txn_fixture_0001 12345: RuntimeError: fictional mapping failure",
};

assert.equal(isStripeSubject("stripe_post_balance_transaction"), true);
assert.equal(isStripeSubject("journal_entry_post"), false);

// Reason kinds, one per mapper branch.
assert.equal(stripeReviewKind(base, reasons.first_run), "first_run");
assert.equal(stripeReviewKind(base, reasons.confirm), "confirm");
assert.equal(stripeReviewKind({ ...base, debit_account: "(undecided)", credit_account: "(undecided)" }, reasons.cpa), "cpa");
// A blocked row created fresh carries debit_account "(blocked)" but no flag;
// an updated one carries blocked: true. Both read as blocked.
assert.equal(stripeReviewKind({ ...base, debit_account: "(blocked)" }, "anything"), "blocked");
assert.equal(stripeReviewKind({ ...base, blocked: true }, reasons.first_run), "blocked");
assert.equal(stripeReviewKind(base, reasons.blocked), "blocked");
assert.equal(stripeReviewKind(base, "something else"), "review");

// Money: minor units per currency.
assert.equal(formatStripeMinor(12345, "usd"), "$123.45");
assert.equal(formatStripeMinor(500, "jpy"), "¥500");
assert.equal(formatStripeMinor(-2500, "eur"), "-€25.00");

const view = stripeReviewView({ ...base, stripe_txn: { ...base.stripe_txn, currency: "eur" } }, reasons.confirm, "USD");
assert.equal(view.currency, "EUR", "the Stripe currency wins over the queue's USD column");
assert.equal(view.amount, "€123.45");
assert.equal(view.btId, "txn_fixture_0001");
assert.equal(view.date, "2026-11-10");

// Render each kind.
const cases: Array<[string, Record<string, unknown>, string]> = [
  ["First-run review", base, reasons.first_run],
  ["Confirm (materiality)", base, reasons.confirm],
  ["CPA flag", { ...base, debit_account: "(undecided)", credit_account: "(undecided)" }, reasons.cpa],
  ["Blocked", { ...base, debit_account: "(blocked)", credit_account: "(blocked)", blocked: true, failure_detail: "RuntimeError: fictional <b>mapping</b> failure" }, reasons.blocked],
];
for (const [label, payload, reasoning] of cases) {
  const html = renderToStaticMarkup(<StripeApprovalDetails payload={payload} reasoning={reasoning} currencyCode="USD" />);
  for (const text of [label, "$123.45", "USD", "txn_fixture_0001", "2026-11-10", "charge"]) {
    assert.ok(html.includes(text), `${label}: missing ${text}`);
  }
  if (label === "Blocked") {
    assert.ok(html.includes("RuntimeError: fictional &lt;b&gt;mapping&lt;/b&gt; failure"), "failure detail shown, escaped");
    assert.ok(html.includes('href="/connectors/stripe"'), "blocked row links to the resolve panel");
    assert.ok(!html.includes("(blocked)"), "placeholder accounts are not shown as a posting");
  } else if (label === "CPA flag") {
    assert.ok(!html.includes("(undecided)"));
  } else {
    assert.ok(html.includes("Dr 1210·Stripe Receivable / Cr 4200·Sales"));
  }
}

// Decide goes through the existing proxy with exactly the DecideRequest body.
type Call = { url: string; init?: RequestInit };
function fakeFetch(status: number, body: unknown, calls: Call[]): FetchLike {
  return async (url, init) => {
    calls.push({ url, init });
    return new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });
  };
}

async function main() {
  for (const decision of ["approve", "reject"] as const) {
    const calls: Call[] = [];
    const ok = await decideApproval(fakeFetch(200, { ok: true, decision: decision === "approve" ? "approved" : "rejected" }, calls), "ws-1", "appr-1", decision);
    assert.equal(ok.ok, true);
    assert.equal(calls.length, 1);
    assert.equal(calls[0].url, "/api/workspaces/ws-1/approvals/appr-1/decide");
    assert.equal(calls[0].init?.method, "POST");
    assert.deepEqual(JSON.parse(String(calls[0].init?.body)), { decision });
  }

  const human = await decideApproval(fakeFetch(403, { detail: "Stripe approval decisions require a human session" }, []), "ws-1", "appr-1", "approve");
  assert.equal(human.ok, false);
  if (!human.ok) {
    assert.equal(human.kind, "human_session");
    assert.equal(human.message, HUMAN_SESSION_MESSAGE);
  }

  const busy = await decideApproval(fakeFetch(409, { detail: "A Stripe sync pass is running for this workspace; retry shortly" }, []), "ws-1", "appr-1", "approve");
  assert.ok(!busy.ok && busy.kind === "retry" && busy.message === RETRY_MESSAGE);

  const other = await decideApproval(fakeFetch(409, { detail: "This Stripe item was resolved elsewhere; reopen the resolution before approving it" }, []), "ws-1", "appr-1", "approve");
  assert.ok(!other.ok && other.kind === "error" && other.message.includes("resolved elsewhere"));

  console.log("Stripe approval rows: passed");
}

main().catch((err) => {
  console.error(err);
  process.exit(1);
});
