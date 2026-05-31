"use client";

import * as Dialog from "@radix-ui/react-dialog";
import { useRouter } from "next/navigation";
import { useState } from "react";
import { CheckCircle2, ExternalLink, Loader2, X } from "lucide-react";
import { cn } from "@/lib/cn";

type ClaimResponse = {
  id: string;
  connector: string;
  label: string;
  accounts_count: number;
  verified: boolean;
};

export function ConnectSimplefinModal({
  workspaceId,
  workspaceName,
  trigger
}: {
  workspaceId: string;
  workspaceName: string;
  trigger: React.ReactNode;
}) {
  const router = useRouter();
  const [open, setOpen] = useState(false);
  const [token, setToken] = useState("");
  const [label, setLabel] = useState("SimpleFIN");
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [success, setSuccess] = useState<ClaimResponse | null>(null);

  function reset() {
    setToken("");
    setLabel("SimpleFIN");
    setError(null);
    setSuccess(null);
    setLoading(false);
  }

  async function submit() {
    setError(null);
    setLoading(true);
    try {
      const res = await fetch(
        `/api/workspaces/${workspaceId}/credentials/simplefin/claim`,
        {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ setup_token: token.trim(), label })
        }
      );
      if (!res.ok) {
        const body = await res.json().catch(() => ({ detail: `HTTP ${res.status}` }));
        throw new Error(body.detail ?? "Claim failed");
      }
      const data = (await res.json()) as ClaimResponse;
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
              Connect SimpleFIN to {workspaceName}
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
                    Connected. {success.accounts_count}{" "}
                    {success.accounts_count === 1 ? "account" : "accounts"} visible.
                  </p>
                  <p className="mt-1 text-ink-secondary">
                    Credential encrypted with AES-256-GCM + per-workspace key derivation
                    and stored in <code className="font-mono text-xs">tenant_credentials</code>.
                    Refresh the dashboard to see live balances + transactions.
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
                Paste the base64-encoded <strong>setup token</strong> from{" "}
                <a
                  href="https://beta-bridge.simplefin.org/"
                  target="_blank"
                  rel="noopener"
                  className="inline-flex items-center gap-1 text-brand-400 underline-offset-2 hover:underline"
                >
                  beta-bridge.simplefin.org
                  <ExternalLink size={11} />
                </a>
                . The server claims it once (single-use), encrypts the resulting access URL,
                and stores it in your workspace. We never log the secret, never write it to
                disk in plaintext, and you can revoke it any time.
              </Dialog.Description>

              <label className="block">
                <span className="text-xs font-medium uppercase tracking-wider text-ink-tertiary">
                  Label
                </span>
                <input
                  type="text"
                  value={label}
                  onChange={(e) => setLabel(e.target.value)}
                  placeholder="e.g. Aaron BofA + USAA"
                  maxLength={256}
                  className="mt-1.5 w-full rounded-md border border-default bg-canvas px-3 py-2 text-sm focus:border-brand-500 focus:outline-none"
                />
              </label>

              <label className="block">
                <span className="text-xs font-medium uppercase tracking-wider text-ink-tertiary">
                  Setup token (base64)
                </span>
                <textarea
                  rows={4}
                  value={token}
                  onChange={(e) => setToken(e.target.value)}
                  placeholder="aHR0cHM6Ly9iZXRhLWJyaWRnZS5zaW1wbGVmaW4ub3JnL3NpbXBsZWZpbi9jbGFpbS8..."
                  className="mt-1.5 w-full resize-none rounded-md border border-default bg-canvas px-3 py-2 font-mono text-[11px] focus:border-brand-500 focus:outline-none"
                />
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
                  disabled={loading || token.trim().length < 10}
                  className={cn(
                    "inline-flex items-center gap-2 rounded-md bg-brand-500 px-4 py-2 text-sm font-medium text-white transition hover:bg-brand-600",
                    (loading || token.trim().length < 10) &&
                      "cursor-not-allowed opacity-60"
                  )}
                >
                  {loading ? <Loader2 size={14} className="animate-spin" /> : null}
                  {loading ? "Claiming..." : "Connect"}
                </button>
              </div>
            </div>
          )}
        </Dialog.Content>
      </Dialog.Portal>
    </Dialog.Root>
  );
}
