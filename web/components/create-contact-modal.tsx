"use client";

// CreateContactModal — manually add a single counterparty (customer / vendor).
//
// Two ways to use it, one component:
//   1. Self-triggered (the /contacts page): pass `trigger`; it owns its open
//      state via Dialog.Trigger.
//   2. Controlled (the reconciliation queue): pass `open` + `onOpenChange` and
//      seed `initialName` / `initialKind` from the transaction's payee. One
//      instance is reused for every row — the open-effect reseeds the form each
//      time it opens, so payee A never bleeds into payee B.
//
// POSTs to the existing, tested create endpoint
//   POST /api/workspaces/{workspaceId}/counterparties   (require_role accountant)
// which UPSERTS (dedupes by contact_id -> email -> display_name), so re-adding
// an existing payee merges rather than duplicating. On success we router.refresh
// so the list / any payee chips pick up the new contact.

import * as Dialog from "@radix-ui/react-dialog";
import { useRouter } from "next/navigation";
import { useEffect, useState } from "react";
import { CheckCircle2, Loader2, UserPlus, X } from "lucide-react";
import { cn } from "@/lib/cn";

type CreateResponse = {
  id: string;
  existed_before: boolean;
  display_name: string;
  kind: string;
};

const KINDS: { value: string; label: string }[] = [
  { value: "customer", label: "Customer — you bill them" },
  { value: "vendor", label: "Vendor — you pay them" },
  { value: "both", label: "Both" }
];

export function CreateContactModal({
  workspaceId,
  trigger,
  open: controlledOpen,
  onOpenChange,
  initialName = "",
  initialKind = "customer",
  sourceTag = "manual"
}: {
  workspaceId: string;
  trigger?: React.ReactNode;
  open?: boolean;
  onOpenChange?: (o: boolean) => void;
  initialName?: string;
  initialKind?: string;
  sourceTag?: string;
}) {
  const router = useRouter();
  const isControlled = controlledOpen !== undefined;
  const [internalOpen, setInternalOpen] = useState(false);
  const open = isControlled ? controlledOpen : internalOpen;

  const [displayName, setDisplayName] = useState(initialName);
  const [kind, setKind] = useState(initialKind);
  const [company, setCompany] = useState("");
  const [email, setEmail] = useState("");
  const [phone, setPhone] = useState("");
  const [address, setAddress] = useState("");
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [success, setSuccess] = useState<CreateResponse | null>(null);

  // Single source of truth for a clean form: every time the dialog opens, seed
  // name/kind from the current props and clear everything else. This is what
  // makes the controlled (queue) reuse correct across different payees.
  useEffect(() => {
    if (open) {
      setDisplayName(initialName);
      setKind(initialKind || "customer");
      setCompany("");
      setEmail("");
      setPhone("");
      setAddress("");
      setError(null);
      setSuccess(null);
      setLoading(false);
    }
  }, [open, initialName, initialKind]);

  function handleOpenChange(o: boolean) {
    if (!isControlled) setInternalOpen(o);
    onOpenChange?.(o);
  }

  function startAnother() {
    setSuccess(null);
    setDisplayName("");
    setKind("customer");
    setCompany("");
    setEmail("");
    setPhone("");
    setAddress("");
    setError(null);
  }

  async function submit() {
    const name = displayName.trim();
    if (!name) {
      setError("A name is required.");
      return;
    }
    setError(null);
    setLoading(true);
    try {
      const res = await fetch(
        `/api/workspaces/${workspaceId}/counterparties`,
        {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            display_name: name,
            kind,
            company: company.trim() || null,
            email: email.trim() || null,
            phone: phone.trim() || null,
            address: address.trim() || null,
            source: sourceTag
          })
        }
      );
      if (!res.ok) {
        const body = await res
          .json()
          .catch(() => ({ detail: `HTTP ${res.status}` }));
        throw new Error(
          typeof body.detail === "string" ? body.detail : "Could not save contact"
        );
      }
      const data = (await res.json()) as CreateResponse;
      setSuccess(data);
      router.refresh();
    } catch (e) {
      setError(e instanceof Error ? e.message : "Unknown error");
    } finally {
      setLoading(false);
    }
  }

  const inputCls =
    "mt-1.5 w-full rounded-md border border-default bg-canvas px-3 py-2 text-sm focus:border-brand-500 focus:outline-none";
  const labelCls =
    "text-xs font-medium uppercase tracking-wider text-ink-tertiary";

  return (
    <Dialog.Root open={open} onOpenChange={handleOpenChange}>
      {trigger ? <Dialog.Trigger asChild>{trigger}</Dialog.Trigger> : null}
      <Dialog.Portal>
        <Dialog.Overlay className="fixed inset-0 z-50 bg-canvas/80 backdrop-blur-sm" />
        <Dialog.Content className="fixed left-1/2 top-1/2 z-50 w-[calc(100vw-2rem)] max-w-lg max-h-[calc(100vh-2rem)] -translate-x-1/2 -translate-y-1/2 overflow-y-auto rounded-lg border border-default bg-surface shadow-card focus:outline-none">
          <div className="flex items-center justify-between border-b border-subtle px-6 py-4">
            <Dialog.Title className="flex items-center gap-2 text-base font-semibold">
              <UserPlus size={16} className="text-brand-400" />
              New contact
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
                    {success.existed_before
                      ? `Updated ${success.display_name}.`
                      : `${success.display_name} added to your contacts.`}
                  </p>
                  <p className="mt-1 text-ink-secondary">
                    {success.existed_before
                      ? "A contact with that name/email already existed, so we merged your details into it instead of creating a duplicate."
                      : `Saved as a ${success.kind}. You'll see them on the Contacts page and when categorizing transactions.`}
                  </p>
                </div>
              </div>
              <div className="flex items-center justify-end gap-2">
                <button
                  onClick={startAnother}
                  className="rounded-md border border-strong bg-surface px-4 py-2 text-sm text-ink-primary hover:bg-elevated"
                >
                  Add another
                </button>
                <button
                  onClick={() => handleOpenChange(false)}
                  className="rounded-md bg-brand-500 px-4 py-2 text-sm font-medium text-white hover:bg-brand-600"
                >
                  Done
                </button>
              </div>
            </div>
          ) : (
            <div className="space-y-4 px-6 py-5">
              <label className="block">
                <span className={labelCls}>Name</span>
                <input
                  type="text"
                  autoFocus
                  value={displayName}
                  onChange={(e) => setDisplayName(e.target.value)}
                  onKeyDown={(e) => {
                    if (e.key === "Enter" && displayName.trim() && !loading) submit();
                  }}
                  placeholder="e.g. Acme Studios LLC"
                  maxLength={256}
                  className={inputCls}
                />
              </label>

              <label className="block">
                <span className={labelCls}>Type</span>
                <select
                  value={kind}
                  onChange={(e) => setKind(e.target.value)}
                  className={inputCls}
                >
                  {KINDS.map((k) => (
                    <option key={k.value} value={k.value}>
                      {k.label}
                    </option>
                  ))}
                </select>
              </label>

              <div className="grid grid-cols-1 gap-4 sm:grid-cols-2">
                <label className="block">
                  <span className={labelCls}>Company</span>
                  <input
                    type="text"
                    value={company}
                    onChange={(e) => setCompany(e.target.value)}
                    placeholder="Optional"
                    maxLength={256}
                    className={inputCls}
                  />
                </label>
                <label className="block">
                  <span className={labelCls}>Email</span>
                  <input
                    type="email"
                    value={email}
                    onChange={(e) => setEmail(e.target.value)}
                    placeholder="Optional"
                    maxLength={256}
                    className={inputCls}
                  />
                </label>
                <label className="block">
                  <span className={labelCls}>Phone</span>
                  <input
                    type="tel"
                    value={phone}
                    onChange={(e) => setPhone(e.target.value)}
                    placeholder="Optional"
                    maxLength={64}
                    className={inputCls}
                  />
                </label>
                <label className="block">
                  <span className={labelCls}>Address</span>
                  <input
                    type="text"
                    value={address}
                    onChange={(e) => setAddress(e.target.value)}
                    placeholder="Optional"
                    maxLength={512}
                    className={inputCls}
                  />
                </label>
              </div>

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
                  disabled={loading || displayName.trim().length < 1}
                  className={cn(
                    "inline-flex items-center gap-2 rounded-md bg-brand-500 px-4 py-2 text-sm font-medium text-white transition hover:bg-brand-600",
                    (loading || displayName.trim().length < 1) &&
                      "cursor-not-allowed opacity-60"
                  )}
                >
                  {loading ? <Loader2 size={14} className="animate-spin" /> : null}
                  {loading ? "Saving…" : "Save contact"}
                </button>
              </div>
            </div>
          )}
        </Dialog.Content>
      </Dialog.Portal>
    </Dialog.Root>
  );
}
