"use client";

import * as Dialog from "@radix-ui/react-dialog";
import { useRouter } from "next/navigation";
import { useState } from "react";
import { CheckCircle2, Loader2, Plus, X } from "lucide-react";
import { cn } from "@/lib/cn";

type AddClientResponse = {
  workspace_id: string;
  slug: string;
  display_name: string;
  your_role: string;
  owner_invitation: {
    email: string;
    delivery?: { status?: string };
  } | null;
};

// CPA-originated client onboarding. Provisions a brand-new client workspace
// with the caller seated as its `owner` (the practice runs the books end to
// end: bank connections, invitations, period close) and, if an owner email is
// given, emails the business owner a `client_owner` invitation (owner rank) so
// the client holds full authority over their own data. Calls the backend
// through the generic /api/proxy passthrough (no per-endpoint route handler).
export function AddClientButton({
  variant = "primary",
}: {
  variant?: "primary" | "ghost";
}) {
  const router = useRouter();
  const [open, setOpen] = useState(false);
  const [businessName, setBusinessName] = useState("");
  const [ownerEmail, setOwnerEmail] = useState("");
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [success, setSuccess] = useState<AddClientResponse | null>(null);

  function reset() {
    setBusinessName("");
    setOwnerEmail("");
    setError(null);
    setSuccess(null);
    setLoading(false);
  }

  async function submit() {
    setError(null);
    setLoading(true);
    try {
      const res = await fetch("/api/proxy/api/me/clients", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          business_name: businessName.trim(),
          owner_email: ownerEmail.trim() || null,
        }),
      });
      if (!res.ok) {
        const body = await res
          .json()
          .catch(() => ({ detail: `HTTP ${res.status}` }));
        throw new Error(body.detail ?? "Could not add client");
      }
      const data = (await res.json()) as AddClientResponse;
      setSuccess(data);
      router.refresh();
    } catch (e) {
      setError(e instanceof Error ? e.message : "Unknown error");
    } finally {
      setLoading(false);
    }
  }

  const trigger =
    variant === "primary" ? (
      <button className="inline-flex items-center gap-1.5 rounded-md bg-brand-500 px-3 py-1.5 text-sm font-medium text-white transition hover:bg-brand-600">
        <Plus size={14} /> Add client
      </button>
    ) : (
      <button className="inline-flex items-center gap-1.5 rounded-md border border-strong bg-surface px-3 py-1.5 text-sm font-medium text-ink-primary transition hover:bg-elevated">
        <Plus size={14} /> Add a client
      </button>
    );

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
              Add a client
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
                    {success.display_name} is set up.
                  </p>
                  <p className="mt-1 text-ink-secondary">
                    {success.owner_invitation
                      ? `You own the workspace and run the books. We emailed an owner invitation to ${success.owner_invitation.email} — they get owner-level access to their own data.`
                      : "You own the workspace. Start the books now; invite the client or your team from Team → Members whenever you like."}
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
                Set up a new client&apos;s books — a business or an individual.
                You&apos;ll be the new workspace&apos;s <strong>owner</strong> —
                connect their bank, add entities, invite your team and close
                periods right away. Add the client&apos;s email to send them an
                invitation so they hold owner-level access to their own books too.
              </Dialog.Description>

              <label className="block">
                <span className="text-xs font-medium uppercase tracking-wider text-ink-tertiary">
                  Client name
                </span>
                <input
                  type="text"
                  value={businessName}
                  onChange={(e) => setBusinessName(e.target.value)}
                  placeholder="e.g. Northwind Remodeling LLC — or Jane Doe"
                  maxLength={160}
                  className="mt-1.5 w-full rounded-md border border-default bg-canvas px-3 py-2 text-sm focus:border-brand-500 focus:outline-none"
                />
              </label>

              <label className="block">
                <span className="text-xs font-medium uppercase tracking-wider text-ink-tertiary">
                  Client email{" "}
                  <span className="text-ink-disabled normal-case">(optional)</span>
                </span>
                <input
                  type="email"
                  value={ownerEmail}
                  onChange={(e) => setOwnerEmail(e.target.value)}
                  placeholder="jane@theirbusiness.com"
                  maxLength={256}
                  className="mt-1.5 w-full rounded-md border border-default bg-canvas px-3 py-2 text-sm focus:border-brand-500 focus:outline-none"
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
                  disabled={loading || businessName.trim().length < 2}
                  className={cn(
                    "inline-flex items-center gap-2 rounded-md bg-brand-500 px-4 py-2 text-sm font-medium text-white transition hover:bg-brand-600",
                    (loading || businessName.trim().length < 2) &&
                      "cursor-not-allowed opacity-60"
                  )}
                >
                  {loading ? <Loader2 size={14} className="animate-spin" /> : null}
                  {loading ? "Setting up…" : "Add client"}
                </button>
              </div>
            </div>
          )}
        </Dialog.Content>
      </Dialog.Portal>
    </Dialog.Root>
  );
}
