"use client";

import * as Dialog from "@radix-ui/react-dialog";
import { useRouter } from "next/navigation";
import { useState } from "react";
import { CheckCircle2, ExternalLink, Loader2, ShieldCheck, X } from "lucide-react";
import { cn } from "@/lib/cn";

type StoreKeyResponse = {
  credential_id: string;
  account_id: string | null;
  business_name: string | null;
  environment: "live" | "test";
  key_display: string;
  entity_id: string;
  default_revenue_account: string;
};

export function ConnectStripeModal({
  workspaceId,
  workspaceName,
  entityId,
  defaultEntityId,
  trigger
}: {
  workspaceId: string;
  workspaceName: string;
  entityId: string | null;
  defaultEntityId: string | null;
  trigger: React.ReactNode;
}) {
  const router = useRouter();
  const [open, setOpen] = useState(false);
  const [apiKey, setApiKey] = useState("");
  const [label, setLabel] = useState("Stripe");
  const [revenueAccount, setRevenueAccount] = useState("4200");
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [success, setSuccess] = useState<StoreKeyResponse | null>(null);

  const targetEntity = entityId ?? defaultEntityId;

  function reset() {
    setApiKey("");
    setLabel("Stripe");
    setRevenueAccount("4200");
    setError(null);
    setSuccess(null);
    setLoading(false);
  }

  async function submit() {
    setError(null);
    if (!targetEntity) {
      setError("Pick a workspace + entity first (top-right selector).");
      return;
    }
    setLoading(true);
    try {
      const res = await fetch(
        `/api/workspaces/${workspaceId}/connectors/stripe/key`,
        {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            api_key: apiKey.trim(),
            entity_id: targetEntity,
            label,
            default_revenue_account: revenueAccount.trim() || "4200"
          })
        }
      );
      if (!res.ok) {
        const body = await res.json().catch(() => ({ detail: `HTTP ${res.status}` }));
        throw new Error(body.detail ?? "Save failed");
      }
      const data = (await res.json()) as StoreKeyResponse;
      setSuccess(data);
      router.refresh();
    } catch (e) {
      setError(e instanceof Error ? e.message : "Unknown error");
    } finally {
      setLoading(false);
    }
  }

  return (
    <Dialog.Root
      open={open}
      onOpenChange={(o) => {
        setOpen(o);
        if (!o) reset();
      }}
    >
      <Dialog.Trigger asChild>{trigger}</Dialog.Trigger>
      <Dialog.Portal>
        <Dialog.Overlay className="fixed inset-0 z-50 bg-canvas/80 backdrop-blur-sm" />
        <Dialog.Content className="fixed left-1/2 top-1/2 z-50 w-full max-w-lg -translate-x-1/2 -translate-y-1/2 rounded-lg border border-default bg-surface shadow-card focus:outline-none">
          <div className="flex items-center justify-between border-b border-subtle px-6 py-4">
            <Dialog.Title className="text-base font-semibold">
              Connect Stripe to {workspaceName}
            </Dialog.Title>
            <Dialog.Close className="rounded p-1 text-ink-tertiary hover:bg-white/5 hover:text-ink-primary">
              <X size={16} />
            </Dialog.Close>
          </div>

          {success ? (
            <div className="space-y-4 px-6 py-6">
              <div className="flex items-start gap-3 rounded-md border border-credit/40 bg-credit/[0.06] px-4 py-3">
                <CheckCircle2 size={18} className="mt-0.5 shrink-0 text-credit" />
                <div className="text-sm">
                  <p className="font-semibold text-ink-primary">
                    Connected to {success.business_name ?? "Stripe account"} ({success.environment}).
                  </p>
                  <p className="mt-1 text-ink-secondary">
                    Stored as <code className="font-mono text-xs">{success.key_display}</code>.
                    The hourly sync worker will pull the first 30 days of charges,
                    refunds, fees, and payouts on its next pass; the first 30 rows
                    queue for your approval.
                  </p>
                  <p className="mt-2 text-ink-secondary">
                    Next: set up the webhook signing secret so real-time events
                    (charge.succeeded, payout.paid, etc.) get recorded.
                  </p>
                </div>
              </div>
              <button
                onClick={() => {
                  setOpen(false);
                  reset();
                  router.refresh();
                }}
                className="w-full rounded-md bg-brand-500 px-4 py-2 text-sm font-medium text-white hover:bg-brand-600"
              >
                Done
              </button>
            </div>
          ) : (
            <div className="space-y-5 px-6 py-5">
              <Dialog.Description className="text-sm leading-relaxed text-ink-secondary">
                Create a <strong>restricted API key</strong> at{" "}
                <a
                  href="https://dashboard.stripe.com/apikeys"
                  target="_blank"
                  rel="noopener"
                  className="inline-flex items-center gap-1 text-brand-400 underline-offset-2 hover:underline"
                >
                  dashboard.stripe.com/apikeys
                  <ExternalLink size={11} />
                </a>{" "}
                with these read-only permissions:
              </Dialog.Description>
              <ul className="rounded-md border border-subtle bg-canvas/40 px-4 py-3 text-[12px] text-ink-secondary">
                <li className="flex items-center gap-2">
                  <ShieldCheck size={12} className="text-credit" />
                  <span>Charges · <strong>Read</strong></span>
                </li>
                <li className="flex items-center gap-2">
                  <ShieldCheck size={12} className="text-credit" />
                  <span>Refunds · <strong>Read</strong></span>
                </li>
                <li className="flex items-center gap-2">
                  <ShieldCheck size={12} className="text-credit" />
                  <span>Payouts · <strong>Read</strong></span>
                </li>
                <li className="flex items-center gap-2">
                  <ShieldCheck size={12} className="text-credit" />
                  <span>Balance transactions · <strong>Read</strong></span>
                </li>
                <li className="flex items-center gap-2">
                  <ShieldCheck size={12} className="text-credit" />
                  <span>Account · <strong>Read</strong></span>
                </li>
              </ul>

              {!targetEntity && (
                <div className="rounded-md border border-pending/40 bg-pending/[0.06] px-3 py-2 text-sm text-pending">
                  Pick an entity in the top-right selector before connecting Stripe.
                </div>
              )}

              <label className="block">
                <span className="text-xs font-medium uppercase tracking-wider text-ink-tertiary">
                  Label
                </span>
                <input
                  type="text"
                  value={label}
                  onChange={(e) => setLabel(e.target.value)}
                  placeholder="e.g. MU Inc Stripe"
                  maxLength={256}
                  className="mt-1.5 w-full rounded-md border border-default bg-canvas px-3 py-2 text-sm focus:border-brand-500 focus:outline-none"
                />
              </label>

              <label className="block">
                <span className="text-xs font-medium uppercase tracking-wider text-ink-tertiary">
                  Restricted API key
                </span>
                <input
                  type="password"
                  value={apiKey}
                  onChange={(e) => setApiKey(e.target.value)}
                  placeholder="rk_live_..."
                  autoComplete="off"
                  className="mt-1.5 w-full rounded-md border border-default bg-canvas px-3 py-2 font-mono text-xs focus:border-brand-500 focus:outline-none"
                />
                <span className="mt-1.5 block text-[11px] text-ink-tertiary">
                  We&apos;ll validate against GET /v1/account before storing. The key is encrypted at rest (AES-256-GCM, per-workspace key derivation) and the plaintext never returns from the server.
                </span>
              </label>

              <label className="block">
                <span className="text-xs font-medium uppercase tracking-wider text-ink-tertiary">
                  Default revenue account (4xxx)
                </span>
                <input
                  type="text"
                  value={revenueAccount}
                  onChange={(e) => setRevenueAccount(e.target.value)}
                  placeholder="4200"
                  maxLength={16}
                  className="mt-1.5 w-full rounded-md border border-default bg-canvas px-3 py-2 font-mono text-sm focus:border-brand-500 focus:outline-none"
                />
                <span className="mt-1.5 block text-[11px] text-ink-tertiary">
                  Account number where Stripe charges credit. MU Inc defaults: 4200 (AI Infrastructure Revenue), 4110 (Fabrikam Design DBA), 4030 (Subscriptions). Override per-charge later via category rules.
                </span>
              </label>

              {error && (
                <div className="rounded-md border border-debit/40 bg-debit/[0.06] px-3 py-2 text-sm text-debit">
                  {error}
                </div>
              )}

              <div className="flex items-center justify-end gap-2 border-t border-subtle pt-4">
                <Dialog.Close className="rounded-md border border-strong bg-surface px-4 py-2 text-sm text-ink-primary hover:bg-elevated">
                  Cancel
                </Dialog.Close>
                <button
                  onClick={submit}
                  disabled={loading || apiKey.trim().length < 10 || !targetEntity}
                  className={cn(
                    "inline-flex items-center gap-2 rounded-md bg-brand-500 px-4 py-2 text-sm font-medium text-white transition hover:bg-brand-600",
                    (loading || apiKey.trim().length < 10 || !targetEntity) &&
                      "cursor-not-allowed opacity-60"
                  )}
                >
                  {loading ? <Loader2 size={14} className="animate-spin" /> : null}
                  {loading ? "Validating..." : "Connect"}
                </button>
              </div>
            </div>
          )}
        </Dialog.Content>
      </Dialog.Portal>
    </Dialog.Root>
  );
}
