import React from "react";
import assert from "node:assert/strict";
import { renderToStaticMarkup } from "react-dom/server";
import type { ApprovalPayload, ManualJournalProposalLine } from "../lib/approvals";
import {
  MANUAL_JOURNAL_INVALID_REASON,
  ManualJournalProposalDetails,
  type ManualJournalActorKind,
  type ManualJournalDecisionMode,
  canReviewManualJournalProposal,
  decideManualJournalProposal,
  expectedManualJournalPairs,
  withoutManualJournalApprovals,
  withoutManualJournalClusterReview,
  withoutManualJournalRecommendations,
} from "../components/manual-journal-proposal-details";

const workspaceId = "11111111-1111-4111-8111-111111111111";
const entityId = "22222222-2222-4222-8222-222222222222";
const requestDigest = "a".repeat(64);
const pairsDigest = "b".repeat(64);

function line(
  code: string,
  name: string,
  side: "debit" | "credit",
  cents: number,
  accountId: string,
): ManualJournalProposalLine {
  const canonical = `${code}\u00b7${name}`;
  return {
    account_code: code,
    side,
    amount_cents: cents,
    canonical_form: canonical,
    resolved_account: {
      account_id: accountId,
      account_code: code,
      canonical_form: canonical,
    },
  };
}

function proposal(
  lines: ManualJournalProposalLine[],
  patch: Partial<ApprovalPayload> = {},
): ApprovalPayload {
  const pairs = expectedManualJournalPairs(lines);
  if (!pairs) throw new Error("fixture lines do not balance");
  const total = lines
    .filter((item) => item.side === "debit")
    .reduce((sum, item) => sum + item.amount_cents, 0);
  return {
    version: 1,
    subject_type: "manual_journal_entry_proposal",
    workspace_id: workspaceId,
    entity_id: entityId,
    idempotency_key: "example-capital-reclass",
    request_digest: requestDigest,
    pairs_digest: pairsDigest,
    effective_date: "2026-11-11",
    memo: "Reclass founder capital within equity",
    lines,
    pairs,
    evidence_links: ["https://example.com/evidence/capital-reclass"],
    proposer_user_uc_uid: "fictional-owner",
    proposer_agent_id: "books-demo",
    reasoning_trace: "Move founder capital within equity.\nCash does not move.",
    model_version: "demo-model",
    total_debit_cents: total,
    ...patch,
  };
}

const workedLines = [
  line("3010", "Owner Contribution", "debit", 125000, "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaa1"),
  line("3000", "Founder Capital", "credit", 125000, "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaa2"),
];
const multiLines = [
  line("3010", "Owner Contribution", "debit", 10000, "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaa1"),
  line("3020", "Additional Paid-in Capital", "debit", 5000, "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaa3"),
  line("3000", "Founder Capital", "credit", 15000, "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaa2"),
];
const worked = proposal(workedLines);
const multi = proposal(multiLines, {
  evidence_links: ["https://example.com/evidence/capital-reclass", "doc:example-capital-note"],
  idempotency_key: "example-multi-line",
});

function decide(
  payload: ApprovalPayload | null,
  actor: { role: string; kind: ManualJournalActorKind },
  mode: ManualJournalDecisionMode = "individual",
  ids: { workspaceId?: string; entityId?: string } = {},
) {
  return decideManualJournalProposal({
    payload,
    workspaceId: ids.workspaceId ?? workspaceId,
    entityId: ids.entityId ?? entityId,
    actor,
    mode,
  });
}

function view(
  payload: ApprovalPayload | null,
  actor: { role: string; kind: ManualJournalActorKind },
  mode: ManualJournalDecisionMode = "individual",
  ids: { workspaceId?: string; entityId?: string } = {},
) {
  return renderToStaticMarkup(
    <ManualJournalProposalDetails
      payload={payload}
      decision={decide(payload, actor, mode, ids)}
      entityLabel="Example Labs"
    />,
  );
}

function assertDisabled(html: string, reason: string) {
  assert.match(html, /<button[^>]*disabled[^>]*>Approve<\/button>/);
  assert.match(html, /<button[^>]*disabled[^>]*>Reject<\/button>/);
  assert.equal(html.includes(reason), true, reason);
  assert.equal(html.includes('role="alert"'), true);
}

const owner = { role: "owner", kind: "human" as const };
const allowed = decide(worked, owner);
assert.equal(allowed.allowed, true);
assert.equal(allowed.showDetails, true);
assert.equal(canReviewManualJournalProposal(worked), true);

const html = view(worked, owner);
for (const text of [
  "Non-cash",
  "Cash does not move",
  "does not post by itself",
  "Example Labs",
  entityId,
  "2026-11-11",
  "Reclass founder capital within equity",
  "$1,250.00",
  "Debit 3010\u00b7Owner Contribution $1,250.00",
  "Credit 3000\u00b7Founder Capital $1,250.00",
  "Debit 3010\u00b7Owner Contribution Credit 3000\u00b7Founder Capital $1,250.00",
  "https://example.com/evidence/capital-reclass",
  "fictional-owner",
  "books-demo",
  "Move founder capital within equity.",
  "Cash does not move.",
  "demo-model",
]) {
  assert.equal(html.includes(text), true, `Missing approval detail: ${text}`);
}
assert.equal(html.includes("<button"), false, "A valid human decision renders no disabled action");
assert.equal(
  html.includes('href="https://example.com/evidence/capital-reclass"'),
  true,
  "Evidence URL must be visible as a link",
);

const escaped = view(proposal(workedLines, { memo: "Reclass <founder>" }), owner);
assert.equal(escaped.includes("Reclass &lt;founder&gt;"), true);
assert.equal(escaped.includes("<founder>"), false, "Memo must be escaped");

const multiHtml = view(multi, owner);
const firstPair = multiHtml.indexOf(
  "Debit 3010\u00b7Owner Contribution Credit 3000\u00b7Founder Capital $100.00",
);
const secondPair = multiHtml.indexOf(
  "Debit 3020\u00b7Additional Paid-in Capital Credit 3000\u00b7Founder Capital $50.00",
);
assert.equal(firstPair >= 0 && secondPair > firstPair, true, "Pairs must follow submitted cursor order");
assert.equal(multiHtml.includes("doc:example-capital-note"), true);
assert.equal(multiHtml.includes('href="doc:example-capital-note"'), false);
assert.equal(multiHtml.includes("$150.00"), true);

const hiddenMemo = "HIDDEN <secret>";
const invalidPayloads: ApprovalPayload[] = [
  proposal(workedLines, { version: 2, memo: hiddenMemo }),
  { ...worked, action: "post", memo: hiddenMemo },
  proposal(workedLines, { subject_type: "journal_entry_post", memo: hiddenMemo }),
  proposal(workedLines, { effective_date: "2026-02-30", memo: hiddenMemo }),
  proposal(workedLines, { memo: "   " }),
  proposal(workedLines, { evidence_links: ["http://example.com/evidence/capital-reclass"] }),
  proposal(workedLines, { evidence_links: ["https://example.com/a", "https://example.com/a"] }),
  proposal(workedLines, { evidence_links: ["javascript:alert(1)"] }),
  proposal(workedLines, { request_digest: "A".repeat(64) }),
  proposal(workedLines, { total_debit_cents: 1 }),
  proposal(workedLines, { proposer_agent_id: { hidden: true } as unknown as string }),
  proposal([
    line("1010", "Operating Account", "debit", 125000, "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaa1"),
    line("3000", "Founder Capital", "credit", 125000, "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaa2"),
  ]),
  proposal([
    line("3010", "Cash Basis Equity", "debit", 125000, "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaa1"),
    line("3000", "Founder Capital", "credit", 125000, "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaa2"),
  ]),
  proposal([
    line("3010", "Owner Contribution", "debit", 125000, "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaa1"),
    line("3010", "Owner Contribution", "credit", 125000, "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaa9"),
  ]),
  { ...worked, lines: [workedLines[0]], memo: hiddenMemo },
  {
    ...multi,
    pairs: [
      { ...multi.pairs![1], amount_cents: 10000 },
      { ...multi.pairs![0], amount_cents: 5000 },
    ],
    memo: hiddenMemo,
  },
  proposal(workedLines, {
    lines: workedLines.map((item) => ({ ...item, amount_cents: 0 })),
  }),
];

for (const invalid of invalidPayloads) {
  const decision = decide(invalid, owner);
  assert.equal(decision.allowed, false);
  assert.equal(decision.showDetails, false);
  assert.equal(canReviewManualJournalProposal(invalid), false);
  const invalidHtml = view(invalid, owner);
  assertDisabled(invalidHtml, MANUAL_JOURNAL_INVALID_REASON);
  assert.equal(invalidHtml.includes(hiddenMemo), false, "Invalid payload details must stay hidden");
  assert.equal(invalidHtml.includes("<secret>"), false);
  assert.equal(invalidHtml.includes("3010"), false, "Invalid lines must not be rendered");
}

const agentHtml = view(worked, { role: "admin", kind: "agent" });
assert.equal(decide(worked, { role: "admin", kind: "agent" }).allowed, false);
assertDisabled(agentHtml, "Agents cannot approve a manual journal proposal.");
assert.equal(agentHtml.includes("$1,250.00"), true, "Agent review can see the lines but cannot approve");
assert.equal(agentHtml.includes("https://example.com/evidence/capital-reclass"), true);

const bulkHtml = view(worked, owner, "bulk");
assert.equal(decide(worked, owner, "bulk").allowed, false);
assertDisabled(bulkHtml, "Bulk decisions cannot approve a manual journal proposal.");
assert.equal(bulkHtml.includes("Debit 3010\u00b7Owner Contribution $1,250.00"), true);

for (const mode of ["skip", "unskip", "recommendation"] as const) {
  const decision = decide(worked, { role: "admin", kind: "human" }, mode);
  assert.equal(decision.allowed, false, mode);
  assertDisabled(view(worked, { role: "admin", kind: "human" }, mode), decision.reason);
}

for (const kind of ["service", "pat"] as const) {
  const decision = decide(worked, { role: "owner", kind });
  assert.equal(decision.allowed, false, kind);
  assertDisabled(view(worked, { role: "owner", kind }), decision.reason);
}

for (const role of ["accountant", "bookkeeper", "platform_admin", "client_owner", "viewer"]) {
  assert.equal(decide(worked, { role, kind: "human" }).allowed, false, role);
}
assert.equal(decide(worked, { role: "admin", kind: "human" }).allowed, true);

const mismatched = view(worked, owner, "individual", { entityId: "33333333-3333-4333-8333-333333333333" });
assertDisabled(
  mismatched,
  "This proposal does not match the approval workspace or entity. Review is unavailable.",
);
assert.equal(mismatched.includes("$1,250.00"), false);

const queue = [
  { id: "manual-1", subject_type: "manual_journal_entry_proposal" },
  { id: "bank-1", subject_type: "bank_transaction_import" },
  { id: "manual-2", subject_type: "manual_journal_entry_proposal" },
];
assert.deepEqual(
  withoutManualJournalApprovals(queue).map((row) => row.id),
  ["bank-1"],
);

const recommendations = [
  {
    approval_id: "manual-1",
    cluster_id: "cluster-a",
    payload: { subject_type: "manual_journal_entry_proposal" },
  },
  {
    approval_id: "bank-1",
    cluster_id: "cluster-a",
    payload: { subject_type: "bank_transaction_import" },
  },
  {
    approval_id: "manual-2",
    cluster_id: "cluster-b",
    payload: { category: "Other" },
  },
];
const recommendationView = withoutManualJournalRecommendations(
  recommendations,
  [
    { cluster_id: "cluster-a", count: 9 },
    { cluster_id: "cluster-b", count: 4 },
  ],
  new Set(["manual-1", "manual-2"]),
);
assert.deepEqual(recommendationView.recommendations.map((row) => row.approval_id), ["bank-1"]);
assert.deepEqual(
  recommendationView.clusters.map((cluster) => [cluster.cluster_id, cluster.count]),
  [["cluster-a", 1]],
);

const clusterReview = withoutManualJournalClusterReview(
  {
    clusters: [
      {
        cluster_id: "cluster-bank",
        items: [{ approval_id: "bank-1" }, { approval_id: "manual-1" }],
        flagged_items: [{ approval_id: "manual-2" }],
        remainder_count: 0,
      },
      {
        cluster_id: "cluster-manual",
        items: [{ approval_id: "manual-1" }],
        flagged_items: [],
        remainder_count: 0,
      },
    ],
    singletons: [{ approval_id: "manual-2" }, { approval_id: "bank-2" }],
  },
  new Set(["manual-1", "manual-2"]),
);
assert.equal(clusterReview.clusters.length, 1);
assert.deepEqual(clusterReview.clusters[0].items.map((item) => item.approval_id), ["bank-1"]);
assert.deepEqual(clusterReview.clusters[0].flagged_items, []);
assert.deepEqual(clusterReview.singletons.map((item) => item.approval_id), ["bank-2"]);

console.log(
  `manual-journal-proposal.smoke: passed (${invalidPayloads.length} invalid payloads, agent and bulk decisions refused)`,
);
