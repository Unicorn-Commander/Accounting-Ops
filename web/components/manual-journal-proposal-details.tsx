import React from "react";
import {
  MANUAL_JOURNAL_ENTRY_PROPOSAL,
  type ApprovalPayload,
  type ManualJournalProposalLine,
  type ManualJournalProposalPair,
  type ManualJournalSide,
} from "@/lib/approvals";

// Version 1 manual journal proposals. The stored pair list is what will post.
// Kit's rule walks debits and credits in submitted order, posting the smaller
// remaining amount and advancing whichever side reaches zero.

const MAX_TOTAL_CENTS = 100_000_000_000;
const MAX_EVIDENCE_LINKS = 50;
const MAX_EVIDENCE_LENGTH = 2048;
const CASH_ACCOUNT_CODES = new Set(["1010", "1011", "1020", "2200"]);
const INTERNAL_EVIDENCE =
  /^(?:doc|document|receipt|internal-document):[A-Za-z0-9][A-Za-z0-9._:/-]{0,255}$/;
const ENVELOPE_KEYS = [
  "version",
  "subject_type",
  "workspace_id",
  "entity_id",
  "idempotency_key",
  "request_digest",
  "pairs_digest",
  "effective_date",
  "memo",
  "lines",
  "pairs",
  "evidence_links",
  "proposer_user_uc_uid",
  "proposer_agent_id",
  "reasoning_trace",
  "model_version",
  "total_debit_cents",
] as const;
const LINE_KEYS = [
  "account_code",
  "side",
  "amount_cents",
  "canonical_form",
  "resolved_account",
] as const;
const RESOLVED_KEYS = ["account_id", "account_code", "canonical_form"] as const;
const PAIR_KEYS = ["debit_account", "credit_account", "amount_cents"] as const;

export const MANUAL_JOURNAL_INVALID_REASON =
  "This manual journal proposal is incomplete or unsupported. Its details must be corrected before approval.";

export type ManualJournalProposalPayload = {
  version: 1;
  subject_type: typeof MANUAL_JOURNAL_ENTRY_PROPOSAL;
  workspace_id: string;
  entity_id: string;
  idempotency_key: string;
  request_digest: string;
  pairs_digest: string;
  effective_date: string;
  memo: string;
  lines: ManualJournalProposalLine[];
  pairs: ManualJournalProposalPair[];
  evidence_links: string[];
  proposer_user_uc_uid: string;
  proposer_agent_id: string | null;
  reasoning_trace: string;
  model_version: string;
  total_debit_cents: number;
};

export type ManualJournalActorKind = "human" | "agent" | "service" | "pat";

export type ManualJournalDecisionMode =
  | "individual"
  | "bulk"
  | "skip"
  | "unskip"
  | "recommendation";

export type ManualJournalDecision = {
  allowed: boolean;
  showDetails: boolean;
  reason: string;
};

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function hasExactKeys(value: Record<string, unknown>, keys: readonly string[]): boolean {
  const present = Object.keys(value);
  return present.length === keys.length && keys.every((key) => present.includes(key));
}

function isIsoDate(value: string): boolean {
  if (!/^\d{4}-\d{2}-\d{2}$/.test(value)) return false;
  const year = Number(value.slice(0, 4));
  const month = Number(value.slice(5, 7));
  const day = Number(value.slice(8, 10));
  const date = new Date(Date.UTC(year, month - 1, day));
  return date.getUTCFullYear() === year
    && date.getUTCMonth() === month - 1
    && date.getUTCDate() === day;
}

function isDigest(value: unknown): value is string {
  return typeof value === "string" && /^[0-9a-f]{64}$/.test(value);
}

function isScopeId(value: unknown): value is string {
  return typeof value === "string" && /^[A-Za-z0-9-]{1,64}$/.test(value);
}

function isAccountCode(value: unknown): value is string {
  return typeof value === "string"
    && value.length >= 1
    && value.length <= 32
    && value === value.trim();
}

function isAccountId(value: unknown): value is string {
  return typeof value === "string" && /^[A-Za-z0-9-]{1,64}$/.test(value);
}

function isPositiveCents(value: unknown): value is number {
  return typeof value === "number"
    && Number.isSafeInteger(value)
    && value > 0
    && value <= MAX_TOTAL_CENTS;
}

function isBoundedText(value: unknown, min: number, max: number): value is string {
  return typeof value === "string"
    && value.length >= min
    && value.length <= max
    && value.trim().length >= min
    && !/[\u0000-\u0008\u000b\u000c\u000e-\u001f]/.test(value);
}

function isCanonicalForm(accountCode: string, value: unknown): value is string {
  if (typeof value !== "string" || value.length > 320) return false;
  const prefix = `${accountCode}\u00b7`;
  if (!value.startsWith(prefix)) return false;
  const name = value.slice(prefix.length);
  if (name.length === 0 || name !== name.trim() || name.length > 256) return false;
  const lowered = name.toLowerCase();
  return !lowered.startsWith("cash")
    && !lowered.startsWith("credit card")
    && !lowered.startsWith("bank");
}

function isEvidenceReference(value: string): boolean {
  if (value.length === 0 || value.length > MAX_EVIDENCE_LENGTH || /\s/.test(value)) return false;
  if (INTERNAL_EVIDENCE.test(value)) return true;
  try {
    const url = new URL(value);
    return url.protocol === "https:" && url.hostname.length > 0;
  } catch {
    return false;
  }
}

function readLine(value: unknown): ManualJournalProposalLine | null {
  if (!isRecord(value) || !hasExactKeys(value, LINE_KEYS)) return null;
  if (!isAccountCode(value.account_code) || CASH_ACCOUNT_CODES.has(value.account_code)) return null;
  if (value.side !== "debit" && value.side !== "credit") return null;
  if (!isPositiveCents(value.amount_cents)) return null;
  if (!isCanonicalForm(value.account_code, value.canonical_form)) return null;
  if (!isRecord(value.resolved_account) || !hasExactKeys(value.resolved_account, RESOLVED_KEYS)) {
    return null;
  }
  const resolved = value.resolved_account;
  if (!isAccountId(resolved.account_id)) return null;
  if (resolved.account_code !== value.account_code) return null;
  if (resolved.canonical_form !== value.canonical_form) return null;
  return {
    account_code: value.account_code,
    side: value.side,
    amount_cents: value.amount_cents,
    canonical_form: value.canonical_form,
    resolved_account: {
      account_id: resolved.account_id,
      account_code: value.account_code,
      canonical_form: value.canonical_form,
    },
  };
}

function accountsAgree(lines: readonly ManualJournalProposalLine[]): boolean {
  const seen = new Map<string, { accountId: string; canonical: string }>();
  for (const line of lines) {
    const prior = seen.get(line.account_code);
    if (prior && (prior.accountId !== line.resolved_account.account_id || prior.canonical !== line.canonical_form)) {
      return false;
    }
    seen.set(line.account_code, {
      accountId: line.resolved_account.account_id,
      canonical: line.canonical_form,
    });
  }
  return true;
}

export function expectedManualJournalPairs(
  lines: readonly ManualJournalProposalLine[],
): ManualJournalProposalPair[] | null {
  const debits = lines.filter((line) => line.side === "debit");
  const credits = lines.filter((line) => line.side === "credit");
  if (debits.length === 0 || credits.length === 0) return null;
  let debitIndex = 0;
  let creditIndex = 0;
  let debitRemaining = debits[0].amount_cents;
  let creditRemaining = credits[0].amount_cents;
  const pairs: ManualJournalProposalPair[] = [];
  while (debitIndex < debits.length && creditIndex < credits.length) {
    const amount = Math.min(debitRemaining, creditRemaining);
    if (!isPositiveCents(amount)) return null;
    pairs.push({
      debit_account: debits[debitIndex].canonical_form,
      credit_account: credits[creditIndex].canonical_form,
      amount_cents: amount,
    });
    debitRemaining -= amount;
    creditRemaining -= amount;
    if (debitRemaining === 0) {
      debitIndex += 1;
      if (debitIndex < debits.length) debitRemaining = debits[debitIndex].amount_cents;
    }
    if (creditRemaining === 0) {
      creditIndex += 1;
      if (creditIndex < credits.length) creditRemaining = credits[creditIndex].amount_cents;
    }
  }
  if (debitIndex !== debits.length || creditIndex !== credits.length) return null;
  if (debitRemaining !== 0 || creditRemaining !== 0) return null;
  return pairs;
}

function sumSide(lines: readonly ManualJournalProposalLine[], side: ManualJournalSide): number {
  return lines.reduce((sum, line) => sum + (line.side === side ? line.amount_cents : 0), 0);
}

function readPairs(value: unknown): ManualJournalProposalPair[] | null {
  if (!Array.isArray(value) || value.length === 0) return null;
  const pairs: ManualJournalProposalPair[] = [];
  for (const item of value) {
    if (!isRecord(item) || !hasExactKeys(item, PAIR_KEYS)) return null;
    if (typeof item.debit_account !== "string" || typeof item.credit_account !== "string") return null;
    if (!isPositiveCents(item.amount_cents)) return null;
    pairs.push({
      debit_account: item.debit_account,
      credit_account: item.credit_account,
      amount_cents: item.amount_cents,
    });
  }
  return pairs;
}

function samePairs(
  stored: readonly ManualJournalProposalPair[],
  expected: readonly ManualJournalProposalPair[],
): boolean {
  return stored.length === expected.length && stored.every((pair, index) => {
    const other = expected[index];
    return pair.debit_account === other.debit_account
      && pair.credit_account === other.credit_account
      && pair.amount_cents === other.amount_cents;
  });
}

export function canReviewManualJournalProposal(
  payload: ApprovalPayload | null | undefined,
): payload is ManualJournalProposalPayload {
  if (!isRecord(payload) || !hasExactKeys(payload, ENVELOPE_KEYS)) return false;
  if (payload.version !== 1 || payload.subject_type !== MANUAL_JOURNAL_ENTRY_PROPOSAL) return false;
  if (!isScopeId(payload.workspace_id) || !isScopeId(payload.entity_id)) return false;
  if (!isBoundedText(payload.idempotency_key, 1, 256)) return false;
  if (!isDigest(payload.request_digest) || !isDigest(payload.pairs_digest)) return false;
  if (typeof payload.effective_date !== "string" || !isIsoDate(payload.effective_date)) return false;
  if (!isBoundedText(payload.memo, 1, 512)) return false;
  if (!isBoundedText(payload.reasoning_trace, 0, 4000)) return false;
  if (!isBoundedText(payload.model_version, 0, 128)) return false;
  if (payload.proposer_agent_id !== null && !isBoundedText(payload.proposer_agent_id, 0, 256)) {
    return false;
  }
  if (!isBoundedText(payload.proposer_user_uc_uid, 1, 256)) return false;
  if (!Array.isArray(payload.lines) || payload.lines.length < 2 || payload.lines.length > 50) return false;
  const lines: ManualJournalProposalLine[] = [];
  for (const item of payload.lines) {
    const line = readLine(item);
    if (!line) return false;
    lines.push(line);
  }
  if (!accountsAgree(lines)) return false;
  const debitTotal = sumSide(lines, "debit");
  const creditTotal = sumSide(lines, "credit");
  if (debitTotal !== creditTotal || !isPositiveCents(debitTotal) || payload.total_debit_cents !== debitTotal) {
    return false;
  }
  if (!Array.isArray(payload.evidence_links) || payload.evidence_links.length > MAX_EVIDENCE_LINKS) {
    return false;
  }
  const seenEvidence = new Set<string>();
  for (const link of payload.evidence_links) {
    if (typeof link !== "string" || seenEvidence.has(link) || !isEvidenceReference(link)) return false;
    seenEvidence.add(link);
  }
  const storedPairs = readPairs(payload.pairs);
  const paired = expectedManualJournalPairs(lines);
  if (!storedPairs || !paired || !samePairs(storedPairs, paired)) return false;
  const pairTotal = storedPairs.reduce((sum, pair) => sum + pair.amount_cents, 0);
  return pairTotal === debitTotal;
}

export function decideManualJournalProposal(input: {
  payload: ApprovalPayload | null | undefined;
  workspaceId: string;
  entityId: string;
  actor: { role: string; kind: ManualJournalActorKind };
  mode: ManualJournalDecisionMode;
}): ManualJournalDecision {
  if (!canReviewManualJournalProposal(input.payload)) {
    return { allowed: false, showDetails: false, reason: MANUAL_JOURNAL_INVALID_REASON };
  }
  const inScope = input.payload.workspace_id === input.workspaceId
    && input.payload.entity_id === input.entityId;
  if (!inScope) {
    return {
      allowed: false,
      showDetails: false,
      reason: "This proposal does not match the approval workspace or entity. Review is unavailable.",
    };
  }
  if (input.mode === "bulk") {
    return {
      allowed: false,
      showDetails: true,
      reason: "Bulk decisions cannot approve a manual journal proposal.",
    };
  }
  if (input.mode === "skip" || input.mode === "unskip") {
    return {
      allowed: false,
      showDetails: true,
      reason: "Skip and unskip cannot decide a manual journal proposal.",
    };
  }
  if (input.mode === "recommendation") {
    return {
      allowed: false,
      showDetails: true,
      reason: "Recommendations cannot approve a manual journal proposal.",
    };
  }
  if (input.mode !== "individual") {
    return {
      allowed: false,
      showDetails: true,
      reason: "This decision path cannot approve a manual journal proposal.",
    };
  }
  if (input.actor.kind === "agent") {
    return {
      allowed: false,
      showDetails: true,
      reason: "Agents cannot approve a manual journal proposal.",
    };
  }
  if (input.actor.kind !== "human") {
    return {
      allowed: false,
      showDetails: true,
      reason: "Service and token sessions cannot approve a manual journal proposal.",
    };
  }
  if (input.actor.role !== "owner" && input.actor.role !== "admin") {
    return {
      allowed: false,
      showDetails: true,
      reason: "Only an owner or admin can approve a manual journal proposal.",
    };
  }
  return { allowed: true, showDetails: true, reason: "" };
}

export function isManualJournalSubject(subjectType: string): boolean {
  return subjectType === MANUAL_JOURNAL_ENTRY_PROPOSAL;
}

export function withoutManualJournalApprovals<T extends { subject_type: string }>(
  rows: readonly T[],
): T[] {
  return rows.filter((row) => !isManualJournalSubject(row.subject_type));
}

export function withoutManualJournalRecommendations<
  R extends {
    approval_id: string;
    cluster_id: string;
    payload: Record<string, unknown> | null;
  },
  C extends { cluster_id: string; count: number },
>(
  recommendations: readonly R[],
  clusters: readonly C[],
  manualApprovalIds: ReadonlySet<string>,
): { recommendations: R[]; clusters: C[] } {
  const kept = recommendations.filter((row) => {
    const subject = row.payload?.subject_type;
    return subject !== MANUAL_JOURNAL_ENTRY_PROPOSAL && !manualApprovalIds.has(row.approval_id);
  });
  const counts = new Map<string, number>();
  for (const row of kept) {
    counts.set(row.cluster_id, (counts.get(row.cluster_id) ?? 0) + 1);
  }
  const nextClusters = clusters
    .filter((cluster) => (counts.get(cluster.cluster_id) ?? 0) > 0)
    .map((cluster) => ({ ...cluster, count: counts.get(cluster.cluster_id) ?? 0 }));
  return { recommendations: kept, clusters: nextClusters };
}

export function withoutManualJournalClusterReview<
  I extends { approval_id: string },
  C extends { items: readonly I[]; flagged_items: readonly I[]; remainder_count: number },
  D extends { clusters: readonly C[]; singletons: readonly I[] },
>(data: D, manualApprovalIds: ReadonlySet<string>): D {
  const clusters = data.clusters
    .map((cluster) => ({
      ...cluster,
      items: cluster.items.filter((item) => !manualApprovalIds.has(item.approval_id)),
      flagged_items: cluster.flagged_items.filter((item) => !manualApprovalIds.has(item.approval_id)),
    }))
    .filter((cluster) =>
      cluster.items.length + cluster.flagged_items.length + cluster.remainder_count > 0
    );
  return {
    ...data,
    clusters,
    singletons: data.singletons.filter((item) => !manualApprovalIds.has(item.approval_id)),
  };
}

function formatCents(cents: number): string {
  return new Intl.NumberFormat("en-US", { style: "currency", currency: "USD" }).format(cents / 100);
}

function DisabledActions({ reason }: { reason: string }) {
  return (
    <div className="flex flex-col items-start gap-2">
      <div className="flex items-center gap-1.5">
        <button
          type="button"
          disabled
          aria-disabled="true"
          className="inline-flex h-8 cursor-not-allowed items-center rounded-md bg-credit/15 px-3 text-xs font-medium text-credit opacity-50"
        >
          Approve
        </button>
        <button
          type="button"
          disabled
          aria-disabled="true"
          className="inline-flex h-8 cursor-not-allowed items-center rounded-md border border-subtle bg-surface px-3 text-xs text-ink-secondary opacity-50"
        >
          Reject
        </button>
      </div>
      <p role="alert" className="text-sm text-ink-secondary">{reason}</p>
    </div>
  );
}

export function ManualJournalProposalDetails({
  payload,
  decision,
  entityLabel,
}: {
  payload: ApprovalPayload | null;
  decision: ManualJournalDecision;
  entityLabel?: string | null;
}) {
  if (!canReviewManualJournalProposal(payload)) {
    return (
      <section aria-label="Manual journal proposal details" className="mt-3 space-y-3">
        <DisabledActions reason={MANUAL_JOURNAL_INVALID_REASON} />
      </section>
    );
  }
  if (!decision.showDetails) {
    return (
      <section aria-label="Manual journal proposal details" className="mt-3 space-y-3">
        <DisabledActions reason={decision.reason || MANUAL_JOURNAL_INVALID_REASON} />
      </section>
    );
  }

  const name = entityLabel?.trim();
  const entity = name ? `${name} (${payload.entity_id})` : payload.entity_id;
  const proposer = payload.proposer_agent_id
    ? `${payload.proposer_user_uc_uid} · agent ${payload.proposer_agent_id}`
    : payload.proposer_user_uc_uid;
  const rows: Array<[string, string]> = [
    ["Entity", entity],
    ["Effective date", payload.effective_date],
    ["Memo", payload.memo],
    ["Total", formatCents(payload.total_debit_cents)],
    ["Proposer", proposer],
  ];
  if (payload.model_version) rows.push(["Model", payload.model_version]);

  return (
    <section aria-label="Manual journal proposal details" className="mt-3 space-y-3">
      <p className="text-sm font-semibold text-ink-primary">Non-cash</p>
      <p className="text-sm text-ink-secondary">
        Cash does not move. A human owner or admin approves this proposal individually.
        It does not post by itself. Approving posts these exact journal rows.
        Changed details require a new proposal.
      </p>
      <dl className="grid gap-x-4 gap-y-2 text-sm sm:grid-cols-[9rem_minmax(0,1fr)]">
        {rows.map(([label, value]) => (
          <div key={label} className="contents">
            <dt className="text-ink-tertiary">{label}</dt>
            <dd className="min-w-0 break-words text-ink-secondary">{value}</dd>
          </div>
        ))}
      </dl>
      <div>
        <h3 className="text-sm font-semibold text-ink-primary">Journal lines</h3>
        <ol aria-label="Journal lines" className="mt-1 space-y-1 text-sm text-ink-secondary">
          {payload.lines.map((line, index) => (
            <li key={`${line.side}-${line.account_code}-${index}`}>
              {`${line.side === "debit" ? "Debit" : "Credit"} ${line.canonical_form} ${formatCents(line.amount_cents)}`}
            </li>
          ))}
        </ol>
      </div>
      <div>
        <h3 className="text-sm font-semibold text-ink-primary">Rows that will post</h3>
        <ol aria-label="Rows that will post" className="mt-1 space-y-1 text-sm text-ink-secondary">
          {payload.pairs.map((pair, index) => (
            <li key={`${pair.debit_account}-${pair.credit_account}-${index}`}>
              {`Debit ${pair.debit_account} Credit ${pair.credit_account} ${formatCents(pair.amount_cents)}`}
            </li>
          ))}
        </ol>
      </div>
      <div>
        <h3 className="text-sm font-semibold text-ink-primary">Evidence</h3>
        <ul aria-label="Evidence links" className="mt-1 space-y-1 text-sm text-ink-secondary">
          {payload.evidence_links.length === 0 ? <li>None</li> : payload.evidence_links.map((link) => (
            <li key={link} className="min-w-0 break-all">
              {isEvidenceReference(link) && link.startsWith("https://")
                ? <a href={link} rel="noreferrer">{link}</a>
                : link}
            </li>
          ))}
        </ul>
      </div>
      {payload.reasoning_trace ? (
        <p className="whitespace-pre-wrap break-words text-sm text-ink-secondary">{payload.reasoning_trace}</p>
      ) : null}
      {!decision.allowed && <DisabledActions reason={decision.reason || MANUAL_JOURNAL_INVALID_REASON} />}
    </section>
  );
}
