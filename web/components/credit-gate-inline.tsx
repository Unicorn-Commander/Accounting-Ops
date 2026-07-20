"use client";

// CreditGateInline — an in-thread version of the credit gate, rendered inside a
// chat conversation when a paid smart AI call returns HTTP 402. Built on the
// pending/amber ErrorState(warning) primitive so it reads as advisory (add
// credits) rather than a hard failure (debit-toned error row). Same two CTAs as
// the modal: get credits / upgrade (external deep-link) and bring your own key.

import Link from "next/link";
import { CreditCard, KeyRound } from "lucide-react";
import { ErrorState } from "@/components/primitives";
import type { CreditGate } from "@/lib/credit-gate";

export function CreditGateInline({ gate }: { gate: CreditGate }) {
  return (
    <ErrorState
      variant="warning"
      title="This feature needs credits"
      description={
        <>
          <p>
            {gate.detail} Add Unicorn Commander credits or bring your own API key
            to turn the smart AI advisors on for this workspace.
          </p>
          <p className="mt-1.5">
            <span className="font-medium text-pending">Bookkeeping stays free</span>{" "}
            — categorizing, reconciliations, and reports never use credits.
          </p>
        </>
      }
      recoveryAction={
        <>
          {gate.upgradeUrl && (
            <a
              href={gate.upgradeUrl}
              target="_blank"
              rel="noopener"
              className="inline-flex items-center justify-center gap-1.5 rounded-md bg-brand-500 px-3 py-1.5 text-xs font-medium text-white shadow-brand-glow transition hover:bg-brand-600"
            >
              <CreditCard size={13} />
              Get credits / Upgrade
            </a>
          )}
          {gate.byokAvailable && (
            <Link
              href="/settings/inference"
              className="inline-flex items-center justify-center gap-1.5 rounded-md border border-strong bg-surface px-3 py-1.5 text-xs font-medium text-ink-primary transition hover:bg-elevated"
            >
              <KeyRound size={13} />
              Use your own key
            </Link>
          )}
        </>
      }
    />
  );
}
