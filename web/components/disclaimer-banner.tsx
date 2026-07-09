// DisclaimerBanner — app-wide, persistent "not tax advice" notice.
//
// A slim, always-visible strip rendered directly under the site header on every
// authenticated page. The loud per-page tax DraftStatusBand covers tax drafts
// specifically; this is the universal, non-dismissible reminder that the app's
// output is assistive draft work — not professional advice — and every figure
// should be verified before it is relied on. Deliberately thin (single line on
// desktop, wraps on mobile) so it reminds without crowding the workspace, and it
// survives `print:` so the disclaimer stays on any printed page.

import { AlertTriangle } from "lucide-react";

export function DisclaimerBanner() {
  return (
    <div
      role="note"
      aria-label="Disclaimer"
      className="border-b border-pending/30 bg-pending/[0.07] px-4 py-1.5 md:px-6 print:border-2 print:border-amber-600 print:bg-amber-50"
    >
      <div className="mx-auto flex w-full max-w-7xl items-center gap-2 text-[11px] leading-tight text-ink-secondary print:text-amber-900">
        <AlertTriangle
          size={13}
          className="shrink-0 text-pending print:text-amber-700"
          aria-hidden="true"
        />
        <p className="min-w-0">
          <span className="font-semibold uppercase tracking-wide text-pending print:text-amber-900">
            Draft — not tax advice
          </span>
          <span className="text-ink-tertiary print:text-amber-700"> · </span>
          <span>
            Verify every figure. Accounting-Ops is assistive software, not a
            substitute for a licensed accountant or tax professional.
          </span>
        </p>
      </div>
    </div>
  );
}
