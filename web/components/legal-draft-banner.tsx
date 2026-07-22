// LegalDraftBanner — shared, non-dismissible notice on every legal page.
//
// docs/legal/ drafts are engineer-authored inputs for counsel, not published
// terms. This banner must stay until counsel signs off and final copy replaces
// the drafts. Distinct from DisclaimerBanner (app-wide "not tax advice").

import { AlertTriangle } from "lucide-react";

export function LegalDraftBanner() {
  return (
    <div
      role="status"
      aria-label="Draft legal notice"
      className="border-b border-pending/40 bg-pending/[0.12] px-4 py-2.5 md:px-6 print:border-2 print:border-amber-600 print:bg-amber-50"
    >
      <div className="mx-auto flex w-full max-w-3xl items-start gap-2.5 text-[12px] leading-snug text-ink-secondary print:text-amber-900">
        <AlertTriangle
          size={15}
          className="mt-0.5 shrink-0 text-pending print:text-amber-700"
          aria-hidden="true"
        />
        <p className="min-w-0">
          <span className="font-semibold uppercase tracking-wide text-pending print:text-amber-900">
            Draft — not in force
          </span>
          <span className="text-ink-tertiary print:text-amber-700"> · </span>
          <span>
            This document has not been reviewed by counsel, is not published as
            a binding agreement, and does not govern use of Accounting-Ops.
            It is a drafting input for legal review.
          </span>
        </p>
      </div>
    </div>
  );
}
