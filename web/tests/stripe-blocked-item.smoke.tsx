import React from "react";
import assert from "node:assert/strict";
import { renderToStaticMarkup } from "react-dom/server";
import { StripeBlockedItemPanel } from "../components/stripe-blocked-item-panel";
import { journalSearchUrl } from "../components/journal-entry-picker";
import { HUMAN_SESSION_MESSAGE, RETRY_MESSAGE, type FetchLike } from "../lib/api-error";
import {
  buildReopenBody,
  buildResolveBody,
  canManageBlockedItem,
  fetchBlockedItemView,
  reasonProblem,
  reopenResolvedItem,
  resolveBlockedItem,
  type BlockedItemView,
} from "../lib/stripe-blocked-item";

// Shape of GET /api/workspaces/{wid}/stripe/blocked-item (app/api/stripe_blocked_item.py).
const view: BlockedItemView = {
  blocked: [{
    bt_id: "txn_blocked_fixture",
    entity_id: "ent-1",
    entity_display_name: "Fictional Co",
    amount: 1250,
    currency: "usd",
    error: "RuntimeError: fictional <mapping> failure",
    failure_count: 4,
    since: "2026-11-10T09:30:00+00:00",
    approval_id: "appr-blocked",
    approval_status: "pending",
  }],
  resolved: [
    {
      bt_id: "txn_resolved_je", entity_id: "ent-1", entity_display_name: "Fictional Co",
      kind: "journal_entry", resolved_by_uc_uid: "fixture-owner", resolved_at: "2026-11-05T10:00:00+00:00",
      journal_entry_id: "je-fixture-1", not_books_relevant_reason: null,
      amount: 900, currency: "eur", error: null, approval_id: "appr-b", audit_id: "audit-b",
    },
    {
      bt_id: "txn_resolved_reason", entity_id: "ent-1", entity_display_name: "Fictional Co",
      kind: "not_books_relevant", resolved_by_uc_uid: "fixture-admin", resolved_at: "2026-11-01T10:00:00+00:00",
      journal_entry_id: null, not_books_relevant_reason: "Fictional duplicate test charge",
      amount: 500, currency: "usd", error: null, approval_id: "appr-a", audit_id: "audit-a",
    },
  ],
  min_reason_chars: 10,
  max_reason_chars: 2000,
};

// ---- Render: owner/admin sees the forms ----
const admin = renderToStaticMarkup(<StripeBlockedItemPanel workspaceId="ws-1" initialView={view} canManage={true} />);
for (const text of [
  "Blocked item", "txn_blocked_fixture", "$12.50", "USD", "RuntimeError: fictional &lt;mapping&gt; failure",
  ">4<", "2026-11-10 09:30 UTC", "Fictional Co",
  'aria-label="Resolve txn_blocked_fixture"', "Booked by a manual journal entry", "Not books-relevant",
  'aria-label="Search journal entries"',
  "Resolved elsewhere", "txn_resolved_je", "€9.00", "fixture-owner", "2026-11-05 10:00 UTC", "je-fixture-1",
  "txn_resolved_reason", "fixture-admin", "Fictional duplicate test charge", "Audit audit-a", "Reopen",
]) assert.ok(admin.includes(text), `admin view missing: ${text}`);
assert.ok(!admin.includes("<mapping>"), "error text is escaped");

// ---- Render: a member sees the state but no forms ----
for (const role of ["member", "viewer", "bookkeeper", "accountant", "cpa"]) {
  assert.equal(canManageBlockedItem(role), false, role);
}
assert.equal(canManageBlockedItem("owner"), true);
assert.equal(canManageBlockedItem("admin"), true);
const member = renderToStaticMarkup(<StripeBlockedItemPanel workspaceId="ws-1" initialView={view} canManage={false} />);
assert.ok(member.includes("txn_blocked_fixture") && member.includes("Fictional duplicate test charge"));
assert.ok(member.includes("Only a workspace owner or admin can resolve a blocked item."));
for (const hidden of ["<form", "Resolve txn_blocked_fixture", "Search journal entries", ">Reopen<", "<textarea"]) {
  assert.ok(!member.includes(hidden), `member view must hide: ${hidden}`);
}

// ---- Render: empty and unavailable ----
const empty = renderToStaticMarkup(<StripeBlockedItemPanel workspaceId="ws-1" initialView={{ ...view, blocked: [], resolved: [] }} canManage={true} />);
assert.ok(empty.includes("Nothing is blocked") && empty.includes("No items have been resolved by hand."));
const down = renderToStaticMarkup(<StripeBlockedItemPanel workspaceId="ws-1" initialView={null} canManage={true} />);
assert.ok(down.includes('role="alert"') && down.includes("unavailable"));

// ---- Reason rule: 10 to MAX chars after trim ----
assert.ok(reasonProblem("   short   "));
assert.equal(reasonProblem("exactly 10"), null);
assert.equal(reasonProblem("x".repeat(2000)), null);
assert.ok(reasonProblem("x".repeat(2001)));

// ---- Exact bodies ----
assert.deepEqual(
  buildResolveBody({ mode: "journal_entry", btId: "txn_blocked_fixture", entityId: "ent-1", journalEntryId: "je-9" }),
  { bt_id: "txn_blocked_fixture", entity_id: "ent-1", journal_entry_id: "je-9" },
);
assert.deepEqual(
  buildResolveBody({ mode: "not_books_relevant", btId: "txn_blocked_fixture", entityId: null, reason: "  Refunded test charge  " }),
  { bt_id: "txn_blocked_fixture", not_books_relevant_reason: "Refunded test charge" },
);
assert.deepEqual(buildReopenBody("txn_resolved_je", "ent-1", " Booked twice by mistake "), {
  bt_id: "txn_resolved_je", entity_id: "ent-1", reason: "Booked twice by mistake",
});

// ---- Picker is limited to this workspace through the scoped proxy ----
assert.equal(
  journalSearchUrl("ws-1", " rent ", "ent-1"),
  "/api/workspaces/ws-1/journal-entries?search=rent&entity_id=ent-1&status_filter=posted&limit=20",
);

type Call = { url: string; init?: RequestInit };
function fakeFetch(status: number, body: unknown, calls: Call[]): FetchLike {
  return async (url, init) => {
    calls.push({ url, init });
    return new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });
  };
}

async function main() {
  {
    const calls: Call[] = [];
    const r = await resolveBlockedItem(fakeFetch(200, { ok: true }, calls), "ws-1",
      { mode: "journal_entry", btId: "txn_blocked_fixture", entityId: "ent-1", journalEntryId: "je-9" });
    assert.equal(r.ok, true);
    assert.equal(calls[0].url, "/api/workspaces/ws-1/stripe/blocked-item/resolve");
    assert.equal(calls[0].init?.method, "POST");
    assert.deepEqual(JSON.parse(String(calls[0].init?.body)),
      { bt_id: "txn_blocked_fixture", entity_id: "ent-1", journal_entry_id: "je-9" });
  }
  {
    const calls: Call[] = [];
    await resolveBlockedItem(fakeFetch(200, { ok: true }, calls), "ws-1",
      { mode: "not_books_relevant", btId: "txn_blocked_fixture", entityId: "ent-1", reason: "Refunded test charge" });
    const sent = JSON.parse(String(calls[0].init?.body));
    assert.deepEqual(sent, { bt_id: "txn_blocked_fixture", entity_id: "ent-1", not_books_relevant_reason: "Refunded test charge" });
    assert.ok(!("journal_entry_id" in sent), "never both justifications");
  }
  {
    const calls: Call[] = [];
    const r = await reopenResolvedItem(fakeFetch(200, { ok: true, warning: "Manual journal entry je-fixture-1 still exists." }, calls),
      "ws-1", "txn_resolved_je", "ent-1", "Booked twice by mistake");
    assert.ok(r.ok && r.data.warning?.includes("je-fixture-1"));
    assert.equal(calls[0].url, "/api/workspaces/ws-1/stripe/blocked-item/reopen");
    assert.deepEqual(JSON.parse(String(calls[0].init?.body)),
      { bt_id: "txn_resolved_je", entity_id: "ent-1", reason: "Booked twice by mistake" });
  }

  // 409 "retry shortly" is a retry note, not an error.
  const busy = await resolveBlockedItem(fakeFetch(409, { detail: "A Stripe sync pass is running for this workspace; retry shortly" }, []),
    "ws-1", { mode: "journal_entry", btId: "t", entityId: null, journalEntryId: "j" });
  assert.ok(!busy.ok && busy.kind === "retry" && busy.message === RETRY_MESSAGE);
  const busyReopen = await reopenResolvedItem(fakeFetch(409, { detail: "A Stripe sync pass is running for this workspace; retry shortly" }, []),
    "ws-1", "t", null, "a valid reason");
  assert.ok(!busyReopen.ok && busyReopen.kind === "retry");

  // 403 human session reads plainly.
  const human = await resolveBlockedItem(fakeFetch(403, { detail: "Resolving or reopening a blocked Stripe item requires a human session" }, []),
    "ws-1", { mode: "journal_entry", btId: "t", entityId: null, journalEntryId: "j" });
  assert.ok(!human.ok && human.kind === "human_session" && human.message === HUMAN_SESSION_MESSAGE);

  // Other refusals keep the backend's words.
  const wrong = await resolveBlockedItem(fakeFetch(409, { detail: "bt_id is not the current blocked Stripe item" }, []),
    "ws-1", { mode: "journal_entry", btId: "t", entityId: null, journalEntryId: "j" });
  assert.ok(!wrong.ok && wrong.kind === "error" && wrong.message === "bt_id is not the current blocked Stripe item");
  const invalid = await resolveBlockedItem(fakeFetch(422, { detail: [{ msg: "String should have at most 2000 characters" }] }, []),
    "ws-1", { mode: "not_books_relevant", btId: "t", entityId: null, reason: "x" });
  assert.ok(!invalid.ok && invalid.message === "String should have at most 2000 characters");

  // The panel's refresh reads the GET proxy.
  const calls: Call[] = [];
  const fresh = await fetchBlockedItemView(fakeFetch(200, view, calls), "ws-1");
  assert.equal(calls[0].url, "/api/workspaces/ws-1/stripe/blocked-item");
  assert.equal(fresh?.blocked[0].bt_id, "txn_blocked_fixture");
  assert.equal(await fetchBlockedItemView(fakeFetch(403, {}, []), "ws-1"), null);

  console.log("Stripe blocked-item panel: passed");
}

main().catch((err) => {
  console.error(err);
  process.exit(1);
});
