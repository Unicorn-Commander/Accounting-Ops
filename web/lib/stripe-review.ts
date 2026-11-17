// Client-safe helpers for Stripe review rows in the approval queue.
//
// Backend source of truth (do not invent shapes):
//   app/connectors/stripe/mapper.py  _enqueue_for_review, enqueue_blocked_balance_transaction
//   app/connectors/stripe/blocked_resolution.py  is_blocked_review_row
// Every Stripe row has subject_type "stripe_post_balance_transaction". A
// "(blocked)" row is the same subject type with payload.blocked = true or
// debit_account "(blocked)"; the review reason is only in `reasoning`.

export const STRIPE_POST_BALANCE_TRANSACTION = "stripe_post_balance_transaction" as const;

export function isStripeSubject(subjectType: string): boolean {
  return subjectType === STRIPE_POST_BALANCE_TRANSACTION;
}

export type StripeReviewKind = "blocked" | "cpa" | "first_run" | "confirm" | "review";

export const STRIPE_REVIEW_LABEL: Record<StripeReviewKind, string> = {
  blocked: "Blocked",
  cpa: "CPA flag",
  first_run: "First-run review",
  confirm: "Confirm (materiality)",
  review: "Review",
};

export type StripeReviewPayload = {
  stripe_balance_txn_id?: unknown;
  stripe_type?: unknown;
  amount_cents?: unknown;
  debit_account?: unknown;
  credit_account?: unknown;
  effective_date?: unknown;
  payee?: unknown;
  description?: unknown;
  blocked?: unknown;
  failure_detail?: unknown;
  stripe_txn?: unknown;
  blocked_resolution?: unknown;
};

/** Mirrors blocked_resolution.is_blocked_review_row, plus the reasoning prefix. */
export function isBlockedStripeRow(payload: StripeReviewPayload | null | undefined, reasoning?: string | null): boolean {
  const p = payload ?? {};
  return Boolean(p.blocked) || p.debit_account === "(blocked)" || (reasoning ?? "").startsWith("(blocked)");
}

/** Which mapper branch queued the row (mapper.py reason strings). */
export function stripeReviewKind(payload: StripeReviewPayload | null | undefined, reasoning?: string | null): StripeReviewKind {
  const r = reasoning ?? "";
  if (isBlockedStripeRow(payload, r)) return "blocked";
  if ((payload ?? {}).debit_account === "(undecided)" || /requires CPA review/i.test(r)) return "cpa";
  if (/^First-\d+-rows review guard/.test(r)) return "first_run";
  if (/^Materiality tier/.test(r)) return "confirm";
  return "review";
}

function str(value: unknown): string | null {
  return typeof value === "string" && value.trim() ? value.trim() : null;
}

function int(value: unknown): number | null {
  return typeof value === "number" && Number.isInteger(value) ? value : null;
}

export function currencyDigits(currency: string): number {
  try {
    return new Intl.NumberFormat("en-US", { style: "currency", currency }).resolvedOptions().maximumFractionDigits ?? 2;
  } catch {
    return 2;
  }
}

/** Stripe amounts are integers in the currency's minor unit. */
export function formatStripeMinor(amount: number, currency: string | null | undefined): string {
  const code = (currency || "USD").toUpperCase();
  const digits = currencyDigits(code);
  const major = amount / 10 ** digits;
  try {
    return new Intl.NumberFormat("en-US", { style: "currency", currency: code }).format(major);
  } catch {
    return `${major.toFixed(digits)} ${code}`;
  }
}

export type StripeReviewView = {
  kind: StripeReviewKind;
  btId: string | null;
  stripeType: string | null;
  amount: string | null;
  currency: string;
  date: string | null;
  debitAccount: string | null;
  creditAccount: string | null;
  failureDetail: string | null;
  resolvedElsewhere: boolean;
};

export function stripeReviewView(
  payload: StripeReviewPayload | null | undefined,
  reasoning: string | null | undefined,
  currencyCode: string | null | undefined,
): StripeReviewView {
  const p = payload ?? {};
  const txn = (p.stripe_txn && typeof p.stripe_txn === "object" ? p.stripe_txn : {}) as Record<string, unknown>;
  const currency = (str(txn.currency) ?? str(currencyCode) ?? "USD").toUpperCase();
  const minor = int(txn.amount) ?? int(p.amount_cents);
  const created = int(txn.created);
  const date = str(p.effective_date) ?? (created !== null ? new Date(created * 1000).toISOString().slice(0, 10) : null);
  const debit = str(p.debit_account);
  const credit = str(p.credit_account);
  const placeholder = (a: string | null) => (a === "(blocked)" || a === "(undecided)" ? null : a);
  return {
    kind: stripeReviewKind(p, reasoning),
    btId: str(p.stripe_balance_txn_id) ?? str(txn.id),
    stripeType: str(p.stripe_type) ?? str(txn.type),
    amount: minor !== null ? formatStripeMinor(minor, currency) : null,
    currency,
    date,
    debitAccount: placeholder(debit),
    creditAccount: placeholder(credit),
    failureDetail: str(p.failure_detail),
    resolvedElsewhere: Boolean(p.blocked_resolution),
  };
}
