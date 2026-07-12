"use client";

// BillingClient — the hosted "Billing & Credits" surface. The real version of
// the "Buy Unicorn Commander credits" tile that /settings/inference stubs as
// coming-soon. Reflects the workspace's entitlement/credit status and offers
// the two ways to unlock the smart AI advisors:
//   1. Get credits / upgrade at Ops-Center (external deep-link via upgrade_url).
//   2. Bring your own API key on /settings/inference.
//
// Community Edition (or a null status — endpoint not deployed / unreachable)
// has no hosted credit path, so it degrades to a BYOK-only pointer. When the
// gate is disabled deployment-wide, smart AI is simply on for everyone.

import Link from "next/link";
import {
  CheckCircle2,
  CreditCard,
  KeyRound,
  Lock,
  Sparkles,
} from "lucide-react";
import { Card } from "@/components/primitives";
import type { BillingStatus } from "@/lib/billing";

const PRIMARY_LINK =
  "inline-flex items-center justify-center gap-1.5 rounded-md bg-brand-500 px-4 py-2 text-sm font-medium text-white shadow-brand-glow transition hover:bg-brand-600";
const SECONDARY_LINK =
  "inline-flex items-center justify-center gap-1.5 rounded-md border border-strong bg-surface px-4 py-2 text-sm font-medium text-ink-primary transition hover:bg-elevated";

export function BillingClient({
  workspaceName,
  isCommunity,
  status,
}: {
  workspaceName: string;
  isCommunity: boolean;
  status: BillingStatus | null;
}) {
  // Community Edition, or the billing endpoint isn't reachable yet → there is
  // no hosted credit/upgrade path, so we point at BYOK only.
  const byokOnly = isCommunity || status === null;

  const canUseSmart = status?.can_use_smart ?? false;
  const entitled = status?.entitled ?? false;
  const byok = status?.byok ?? false;
  const gateEnabled = status?.gate_enabled ?? true;
  const upgradeUrl = status?.upgrade_url ?? null;

  // The Ops-Center deep-link CTA only makes sense when there's a live hosted
  // gate to satisfy and a URL to send the user to.
  const showUpgrade = !byokOnly && gateEnabled && !!upgradeUrl;

  // Status hero — icon, tone, headline, and explanation for the current state.
  const hero = (() => {
    if (byokOnly) {
      return {
        icon: <Sparkles size={18} />,
        ring: "bg-brand-500/10 text-brand-400",
        title: "Power AI with your own key",
        body: (
          <>
            {isCommunity
              ? "This is the self-hosted Community Edition — there's no hosted credit plan."
              : "Hosted credits aren't reachable right now."}{" "}
            Bring your own provider API key and the smart AI advisors run on it,
            billed by the provider directly.
          </>
        ),
      };
    }
    if (canUseSmart) {
      return {
        icon: <CheckCircle2 size={18} />,
        ring: "bg-credit/10 text-credit",
        title: "Smart AI is on",
        body: entitled ? (
          <>
            <strong>{workspaceName}</strong> has an Accounting-Ops plan, so the
            CFO advisor and tax specialists are ready to use.
          </>
        ) : byok ? (
          <>
            Smart AI in <strong>{workspaceName}</strong> runs on the API key
            configured for this workspace, billed by the provider directly.
          </>
        ) : (
          <>
            Smart AI is available on this deployment — nothing to buy for{" "}
            <strong>{workspaceName}</strong>.
          </>
        ),
      };
    }
    return {
      icon: <Sparkles size={18} />,
      ring: "bg-pending/[0.12] text-pending",
      title: "Smart AI is off for this workspace",
      body: (
        <>
          The CFO advisor and tax specialists run on paid inference. Add Unicorn
          Commander credits or bring your own API key to switch them on for{" "}
          <strong>{workspaceName}</strong>.
        </>
      ),
    };
  })();

  return (
    <div className="space-y-6">
      {/* Status hero + CTAs ---------------------------------------------- */}
      <Card>
        <div className="flex flex-col gap-4 sm:flex-row sm:items-start sm:justify-between">
          <div className="flex items-start gap-3">
            <span
              className={`mt-0.5 flex h-10 w-10 shrink-0 items-center justify-center rounded-full ${hero.ring}`}
            >
              {hero.icon}
            </span>
            <div className="min-w-0">
              <p className="text-sm font-semibold text-ink-primary">
                {hero.title}
              </p>
              <p className="mt-1 text-xs leading-relaxed text-ink-secondary">
                {hero.body}
              </p>
            </div>
          </div>

          <div className="flex shrink-0 flex-col-reverse gap-2 sm:flex-row sm:items-center">
            {showUpgrade ? (
              <>
                <Link href="/settings/inference" className={SECONDARY_LINK}>
                  <KeyRound size={14} />
                  Use your own key
                </Link>
                <a
                  href={upgradeUrl!}
                  target="_blank"
                  rel="noopener"
                  className={PRIMARY_LINK}
                >
                  <CreditCard size={14} />
                  Get credits / Upgrade
                </a>
              </>
            ) : (
              <Link href="/settings/inference" className={PRIMARY_LINK}>
                <KeyRound size={14} />
                {byok ? "Manage your key" : "Set up your key"}
              </Link>
            )}
          </div>
        </div>
      </Card>

      {/* Reassurance — bookkeeping is always free ------------------------ */}
      <div className="rounded-md border border-pending/30 bg-pending/[0.07] px-4 py-3 text-xs leading-relaxed text-ink-secondary">
        <span className="font-semibold text-pending">
          Free bookkeeping is never blocked.
        </span>{" "}
        Categorizing transactions, reconciliations, journal entries, and reports
        run without credits. Only the smart AI advisors — the CFO agent and the
        tax specialists — draw on credits or your own key.
      </div>

      {/* Ways to pay explainer ------------------------------------------- */}
      <Card className="bg-canvas/40">
        <h2 className="text-sm font-semibold text-ink-primary">
          Two ways to power smart AI
        </h2>
        <div className="mt-3 grid grid-cols-1 gap-3 sm:grid-cols-2">
          <div className="flex items-start gap-3 rounded-lg border border-subtle bg-white/[0.02] px-4 py-3">
            <span className="mt-0.5 flex h-8 w-8 shrink-0 items-center justify-center rounded-full bg-brand-500/10 text-brand-400">
              <CreditCard size={16} />
            </span>
            <div className="min-w-0">
              <p className="text-sm font-medium text-ink-primary">
                Unicorn Commander credits
              </p>
              <p className="mt-1 text-xs leading-relaxed text-ink-tertiary">
                Prepaid credits with a hard spend cap — we handle the provider
                billing. {byokOnly ? "Available on the hosted plan." : "Top up any time."}
              </p>
            </div>
          </div>
          <div className="flex items-start gap-3 rounded-lg border border-subtle bg-white/[0.02] px-4 py-3">
            <span className="mt-0.5 flex h-8 w-8 shrink-0 items-center justify-center rounded-full bg-brand-500/10 text-brand-400">
              <KeyRound size={16} />
            </span>
            <div className="min-w-0">
              <div className="flex items-center gap-2">
                <p className="text-sm font-medium text-ink-primary">
                  Bring your own key
                </p>
                {byok && (
                  <span className="inline-flex items-center gap-1 rounded-full bg-credit/15 px-2 py-0.5 text-[10px] font-medium uppercase tracking-wider text-credit">
                    <CheckCircle2 size={9} />
                    Active
                  </span>
                )}
              </div>
              <p className="mt-1 text-xs leading-relaxed text-ink-tertiary">
                Paste an Anthropic or OpenRouter key and the provider bills you
                directly.{" "}
                <Link
                  href="/settings/inference"
                  className="text-brand-400 hover:underline"
                >
                  Manage on Inference &amp; billing
                </Link>
                .
              </p>
            </div>
          </div>
        </div>
      </Card>

      {/* No-gate note — smart AI is on for everyone on this deployment --- */}
      {!byokOnly && !gateEnabled && (
        <div className="flex items-center gap-2 text-xs text-ink-tertiary">
          <Lock size={11} />
          Credit metering is disabled on this deployment — smart AI is available
          to every workspace.
        </div>
      )}
    </div>
  );
}
