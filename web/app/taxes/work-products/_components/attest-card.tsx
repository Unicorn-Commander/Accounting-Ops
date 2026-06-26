"use client";

import { useState } from "react";
import { Check, Info, Loader2, ShieldCheck } from "lucide-react";
import { Card } from "@/components/primitives";
import { cn } from "@/lib/cn";
import type { WorkProductStatus } from "@/lib/forms";

// ---------------------------------------------------------------------------
// AttestCard — the self-certify affordance, generalized from the W-4P screen
// to any form kind. Three mutually-exclusive states:
//
//   • canSelfAttest          → the certify checkbox + "Self-certify" button
//   • proRequired (draft)    → a calm "a licensed pro review is required" note
//   • status !== "draft"     → a calm "certified" confirmation
//
// The card renders nothing in any other combination (e.g. autonomous_off on a
// draft) so callers can drop it in unconditionally.
// ---------------------------------------------------------------------------

export type AttestCardProps = {
  /** Performs the attest call; resolves on success, rejects with an Error. */
  onAttest: () => Promise<void>;
  /** True while the attest request is in flight. */
  pending: boolean;
  /** Policy allows self-attestation AND the form is still a draft. */
  canSelfAttest: boolean;
  /** Policy requires a licensed-professional review. */
  proRequired: boolean;
  /** Current work-product status. */
  status: WorkProductStatus;
};

export function AttestCard({
  onAttest,
  pending,
  canSelfAttest,
  proRequired,
  status,
}: AttestCardProps) {
  const [confirmChecked, setConfirmChecked] = useState(false);
  const [attestError, setAttestError] = useState<string | null>(null);

  async function handleAttest() {
    if (!confirmChecked || pending) return;
    setAttestError(null);
    try {
      await onAttest();
      setConfirmChecked(false);
    } catch (e) {
      setAttestError(e instanceof Error ? e.message : "Could not self-certify");
    }
  }

  // ----- Self-certify (policy allows + draft) ------------------------------
  if (canSelfAttest) {
    return (
      <Card>
        <div className="flex items-start gap-3">
          <div className="flex h-9 w-9 shrink-0 items-center justify-center rounded-md bg-elevated text-brand-400 ring-1 ring-inset ring-brand-500/30">
            <ShieldCheck size={18} />
          </div>
          <div className="min-w-0 flex-1">
            <h3 className="text-sm font-semibold">Self-certify this form</h3>
            <p className="mt-1 text-xs leading-relaxed text-ink-secondary">
              No licensed professional is required for your workspace, so you can
              certify this form yourself. Certifying removes the DRAFT watermark
              — it does <strong>not</strong> file anything. You still complete
              the last-mile steps (sign and submit) below.
            </p>

            <label className="mt-4 flex cursor-pointer items-start gap-2.5">
              <input
                type="checkbox"
                checked={confirmChecked}
                onChange={(e) => setConfirmChecked(e.target.checked)}
                className="mt-0.5 h-4 w-4 shrink-0 rounded border-default bg-elevated accent-brand-500"
              />
              <span className="text-xs leading-relaxed text-ink-secondary">
                I have reviewed every line of this form and confirm the
                information is correct to the best of my knowledge.
              </span>
            </label>

            <div className="mt-4 flex items-center gap-3">
              <button
                onClick={handleAttest}
                disabled={!confirmChecked || pending}
                className={cn(
                  "inline-flex items-center gap-1.5 rounded-md bg-credit/15 px-3 py-1.5 text-xs font-medium text-credit transition hover:bg-credit/25 disabled:cursor-not-allowed disabled:opacity-50",
                  pending && "cursor-wait"
                )}
              >
                {pending ? (
                  <Loader2 size={13} className="animate-spin" />
                ) : (
                  <Check size={13} />
                )}
                Self-certify
              </button>
              {attestError && (
                <span className="text-[11px] text-debit" title={attestError}>
                  {attestError}
                </span>
              )}
            </div>
          </div>
        </div>
      </Card>
    );
  }

  // ----- Already certified — calm confirmation -----------------------------
  if (status !== "draft") {
    return (
      <div className="flex items-start gap-2 rounded-lg border border-credit/30 bg-credit/[0.06] px-4 py-3">
        <Check size={14} className="mt-0.5 shrink-0 text-credit" />
        <p className="text-[11px] leading-relaxed text-credit">
          {status === "self_approved"
            ? "You self-certified this form — the DRAFT watermark is removed. Complete the sign and submit steps below."
            : status === "pro_reviewed"
              ? "This form has been reviewed by a licensed professional. Complete the sign and submit steps below."
              : "This form is finalized. Complete the sign and submit steps below."}
        </p>
      </div>
    );
  }

  // ----- pro_required gate (draft) — calm note, NOT an error ---------------
  if (proRequired) {
    return (
      <div className="flex items-start gap-2 rounded-lg border border-default bg-elevated/50 px-4 py-3">
        <Info size={14} className="mt-0.5 shrink-0 text-ink-tertiary" />
        <p className="text-[11px] leading-relaxed text-ink-secondary">
          A licensed professional review is required before this can be
          finalized. If no professional is available, an admin can enable
          self-certification in Settings.
        </p>
      </div>
    );
  }

  // No certify affordance applies (e.g. autonomous_off on a draft).
  return null;
}
