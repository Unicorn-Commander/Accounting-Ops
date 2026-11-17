// Readable body for a Stripe review row in the approval queue
// (subject_type "stripe_post_balance_transaction", including "(blocked)" rows).
// Presentational only: Approve / Reject stay in ApprovalActions, which calls
// the existing decide proxy.

import Link from "next/link";
import { StatusChip } from "@/components/primitives";
import { STRIPE_REVIEW_LABEL, stripeReviewView, type StripeReviewPayload } from "@/lib/stripe-review";

const KIND_TONE = {
  blocked: "blocked",
  cpa: "debit",
  first_run: "pending",
  confirm: "pending",
  review: "ink",
} as const;

export function StripeApprovalDetails({
  payload,
  reasoning,
  currencyCode,
}: {
  payload: StripeReviewPayload | null | undefined;
  reasoning: string | null | undefined;
  currencyCode: string | null | undefined;
}) {
  const v = stripeReviewView(payload, reasoning, currencyCode);
  return (
    <div className="mt-3 rounded-md border border-subtle bg-canvas/40 p-3 text-[12px]" data-stripe-kind={v.kind}>
      <div className="flex flex-wrap items-center gap-2">
        <StatusChip tone={KIND_TONE[v.kind]}>{STRIPE_REVIEW_LABEL[v.kind]}</StatusChip>
        {v.resolvedElsewhere && <StatusChip tone="ink">Resolved elsewhere</StatusChip>}
      </div>
      <dl className="mt-2 grid grid-cols-[max-content_1fr] gap-x-4 gap-y-1 text-ink-secondary">
        <dt className="text-ink-tertiary">Amount</dt>
        <dd className="font-mono text-ink-primary">{v.amount ?? "Unknown"} <span className="text-ink-tertiary">{v.currency}</span></dd>
        <dt className="text-ink-tertiary">Balance transaction</dt>
        <dd className="break-all font-mono">{v.btId ?? "Unknown"}</dd>
        {v.stripeType && (<><dt className="text-ink-tertiary">Stripe type</dt><dd className="font-mono">{v.stripeType}</dd></>)}
        <dt className="text-ink-tertiary">Date</dt>
        <dd className="font-mono">{v.date ?? "Unknown"}</dd>
        {v.debitAccount && v.creditAccount && (
          <>
            <dt className="text-ink-tertiary">Posting</dt>
            <dd className="font-mono">Dr {v.debitAccount} / Cr {v.creditAccount}</dd>
          </>
        )}
        {v.kind === "blocked" && v.failureDetail && (
          <>
            <dt className="text-ink-tertiary">Sync error</dt>
            <dd className="break-words font-mono text-debit">{v.failureDetail}</dd>
          </>
        )}
      </dl>
      {v.kind === "blocked" && (
        <p className="mt-2 text-ink-tertiary">
          The sync stopped on this item. Approve retries it through the mapper; if it was handled
          another way, resolve it on the{" "}
          <Link href="/connectors/stripe" className="text-brand-400 underline-offset-2 hover:underline">
            Stripe settings page
          </Link>
          .
        </p>
      )}
      {v.kind === "cpa" && (
        <p className="mt-2 text-ink-tertiary">No default mapping for this Stripe type. A CPA should decide how it is booked.</p>
      )}
    </div>
  );
}
