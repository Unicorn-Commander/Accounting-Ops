"use client";

// CreditGateModal — a "show-from-anywhere" overlay that pops when a paid smart
// AI call returns HTTP 402. Mounted once in AppShell (next to <GlobalToast />)
// and driven by the credit-gate event bus in lib/credit-gate.ts. Chrome mirrors
// the BYOK KeyModal in app/settings/inference/page-client.tsx.
//
// A user resolves the gate two ways:
//   1. Get credits / upgrade at Ops-Center (external deep-link, when provided).
//   2. Bring your own API key on /settings/inference.
// Free bookkeeping is never gated — the copy says so plainly.

import { useEffect, useState } from "react";
import Link from "next/link";
import * as Dialog from "@radix-ui/react-dialog";
import { CreditCard, KeyRound, Sparkles, X } from "lucide-react";
import { subscribeCreditGate, type CreditGate } from "@/lib/credit-gate";

export function CreditGateModal() {
  const [gate, setGate] = useState<CreditGate | null>(null);
  const open = gate !== null;

  useEffect(() => subscribeCreditGate(setGate), []);

  function close() {
    setGate(null);
  }

  const hasCta = !!gate?.upgradeUrl || !!gate?.byokAvailable;

  return (
    <Dialog.Root
      open={open}
      onOpenChange={(o) => {
        if (!o) close();
      }}
    >
      <Dialog.Portal>
        <Dialog.Overlay className="fixed inset-0 z-50 bg-canvas/80 backdrop-blur-sm" />
        <Dialog.Content className="fixed left-1/2 top-1/2 z-50 max-h-[90vh] w-full max-w-lg -translate-x-1/2 -translate-y-1/2 overflow-y-auto rounded-lg border border-default bg-surface shadow-card focus:outline-none">
          <div className="flex items-center justify-between border-b border-subtle px-6 py-4">
            <Dialog.Title className="flex items-center gap-2.5 text-base font-semibold">
              <span className="flex h-7 w-7 shrink-0 items-center justify-center rounded-full bg-pending/[0.12] text-pending">
                <Sparkles size={15} />
              </span>
              This feature needs credits
            </Dialog.Title>
            <Dialog.Close
              onClick={close}
              className="rounded p-1 text-ink-tertiary hover:bg-white/5 hover:text-ink-primary"
            >
              <X size={16} />
            </Dialog.Close>
          </div>

          <div className="space-y-4 px-6 py-5">
            <Dialog.Description className="text-sm leading-relaxed text-ink-secondary">
              {gate?.detail ??
                "The CFO advisor and tax specialists run on paid inference."}{" "}
              Add Unicorn Commander credits — or bring your own API key — to turn
              the smart AI advisors on for this workspace.
            </Dialog.Description>

            <div className="rounded-md border border-pending/30 bg-pending/[0.07] px-3 py-2.5 text-xs leading-relaxed text-ink-secondary">
              <span className="font-semibold text-pending">
                Bookkeeping stays free.
              </span>{" "}
              Categorizing transactions, reconciliations, and reports never use
              credits — only the smart AI advisors do.
            </div>

            {hasCta && (
              <div className="flex flex-col-reverse gap-2 border-t border-subtle pt-4 sm:flex-row sm:justify-end">
                {gate?.byokAvailable && (
                  <Link
                    href="/settings/inference"
                    onClick={close}
                    className="inline-flex items-center justify-center gap-1.5 rounded-md border border-strong bg-surface px-4 py-2 text-sm font-medium text-ink-primary transition hover:bg-elevated"
                  >
                    <KeyRound size={14} />
                    Use your own key
                  </Link>
                )}
                {gate?.upgradeUrl && (
                  <a
                    href={gate.upgradeUrl}
                    target="_blank"
                    rel="noopener"
                    className="inline-flex items-center justify-center gap-1.5 rounded-md bg-brand-500 px-4 py-2 text-sm font-medium text-white shadow-brand-glow transition hover:bg-brand-600"
                  >
                    <CreditCard size={14} />
                    Get credits / Upgrade
                  </a>
                )}
              </div>
            )}
          </div>
        </Dialog.Content>
      </Dialog.Portal>
    </Dialog.Root>
  );
}
