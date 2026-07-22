"use client";

import { useState } from "react";
import * as Dialog from "@radix-ui/react-dialog";
import {
  CheckCircle2,
  CreditCard,
  KeyRound,
  Loader2,
  Lock,
  Sparkles,
  Trash2,
  X,
} from "lucide-react";
import { Card } from "@/components/primitives";
import { cn } from "@/lib/cn";
import type { ByokProvider, ByokStatus } from "@/lib/inference-settings";

const PROVIDER_LABEL: Record<string, string> = {
  anthropic: "Anthropic",
  openrouter: "OpenRouter",
};

function providerLabel(provider: string | null): string {
  if (!provider) return "your own";
  return PROVIDER_LABEL[provider] ?? provider;
}

async function readError(res: Response): Promise<string> {
  const d = await res.json().catch(() => null);
  if (d?.detail) return String(d.detail);
  if (d?.error) return String(d.error);
  return `${res.status} ${res.statusText}`;
}

export function InferenceSettingsClient({
  workspaceId,
  workspaceName,
  initialStatus,
  canRemove,
}: {
  workspaceId: string;
  workspaceName: string;
  initialStatus: ByokStatus;
  canRemove: boolean;
}) {
  const [status, setStatus] = useState<ByokStatus>(initialStatus);
  const [editOpen, setEditOpen] = useState(false);
  const [confirmRemove, setConfirmRemove] = useState(false);
  const [removing, setRemoving] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const url = `/api/workspaces/${workspaceId}/inference/byok`;

  async function remove() {
    setRemoving(true);
    setError(null);
    try {
      const res = await fetch(url, { method: "DELETE" });
      if (!res.ok) throw new Error(await readError(res));
      setStatus({
        configured: false,
        provider: null,
        key_last4: null,
        base_url: null,
      });
      setConfirmRemove(false);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setRemoving(false);
    }
  }

  return (
    <div className="space-y-6">
      {error && (
        <div className="rounded-md border border-debit/40 bg-debit/[0.06] px-3 py-2 text-sm text-debit">
          {error}
        </div>
      )}

      {/* Current mode card ----------------------------------------------- */}
      <Card>
        <div className="flex flex-col gap-4 sm:flex-row sm:items-start sm:justify-between">
          <div className="flex items-start gap-3">
            <span className="mt-0.5 flex h-10 w-10 shrink-0 items-center justify-center rounded-full bg-brand-500/10 text-brand-400">
              {status.configured ? <KeyRound size={18} /> : <Sparkles size={18} />}
            </span>
            <div className="min-w-0">
              {status.configured ? (
                <>
                  <p className="text-sm font-semibold text-ink-primary">
                    Using your own {providerLabel(status.provider)} key
                    <span className="ml-2 font-mono text-xs text-ink-tertiary">
                      ••••{status.key_last4 ?? "????"}
                    </span>
                  </p>
                  <p className="mt-1 text-xs leading-relaxed text-ink-secondary">
                    AI usage in <strong>{workspaceName}</strong> runs on this
                    key and is billed by {providerLabel(status.provider)}{" "}
                    directly — we don&apos;t meter or mark it up.
                    {status.base_url ? (
                      <>
                        {" "}
                        Endpoint:{" "}
                        <code className="font-mono text-[11px]">
                          {status.base_url}
                        </code>
                        .
                      </>
                    ) : null}
                  </p>
                </>
              ) : (
                <>
                  <p className="text-sm font-semibold text-ink-primary">
                    Using Unicorn Commander credits
                  </p>
                  <p className="mt-1 text-xs leading-relaxed text-ink-secondary">
                    AI usage in <strong>{workspaceName}</strong> draws on your
                    Unicorn Commander credit balance. Bring your own API key to
                    bill the provider directly instead.
                  </p>
                </>
              )}
            </div>
          </div>

          <div className="flex shrink-0 flex-wrap items-center gap-2">
            <button
              type="button"
              onClick={() => {
                setError(null);
                setEditOpen(true);
              }}
              className="inline-flex items-center gap-1.5 rounded-md bg-brand-500 px-4 py-2 text-sm font-medium text-white shadow-brand-glow transition hover:bg-brand-600"
            >
              <KeyRound size={14} />
              {status.configured ? "Replace key" : "Bring your own key"}
            </button>
            {status.configured && canRemove ? (
              <button
                type="button"
                onClick={() => setConfirmRemove(true)}
                className="inline-flex items-center gap-1.5 rounded-md border border-default px-3 py-2 text-sm text-ink-secondary transition hover:border-debit/60 hover:text-debit"
              >
                <Trash2 size={14} />
                Remove
              </button>
            ) : null}
          </div>
        </div>
      </Card>

      {/* Plain-language explainer ---------------------------------------- */}
      <Card className="bg-canvas/40">
        <h2 className="text-sm font-semibold text-ink-primary">
          Bring your own API key
        </h2>
        <p className="mt-1.5 text-xs leading-relaxed text-ink-secondary">
          Paste a key from a supported provider and your AI usage is billed by
          that provider directly, not metered by us. Your key is encrypted at
          rest, never shown again, and never leaves your workspace. Remove it any
          time to fall back to Unicorn Commander credits.
        </p>
        <ul className="mt-3 space-y-1.5 text-xs text-ink-tertiary">
          <li>
            <span className="font-medium text-ink-secondary">Anthropic</span> —
            a Claude API key (<code className="font-mono text-[11px]">sk-ant-…</code>
            ).
          </li>
          <li>
            <span className="font-medium text-ink-secondary">OpenRouter</span> —
            an OpenRouter key (<code className="font-mono text-[11px]">sk-or-…</code>
            ) for breadth across models.
          </li>
        </ul>
      </Card>

      {/* Coming-soon placeholders ---------------------------------------- */}
      <div>
        <p className="mb-2 text-[10px] font-medium uppercase tracking-wider text-ink-tertiary">
          More ways to pay (coming soon)
        </p>
        <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
          <ComingSoonRow
            icon={<CreditCard size={16} />}
            title="Buy Unicorn Commander credits"
            description="Top up prepaid credits and we handle the provider billing with a hard spend cap."
          />
          <ComingSoonRow
            icon={<Sparkles size={16} />}
            title="Connect your Claude subscription"
            description="Apply your Claude Pro/Max plan's monthly Agent-SDK allowance to Accounting-Ops."
          />
        </div>
      </div>

      {/* Save / replace modal -------------------------------------------- */}
      <KeyModal
        open={editOpen}
        onClose={() => setEditOpen(false)}
        workspaceName={workspaceName}
        url={url}
        onSaved={(s) => setStatus(s)}
      />

      {/* Remove confirm -------------------------------------------------- */}
      <Dialog.Root
        open={confirmRemove}
        onOpenChange={(o) => !o && setConfirmRemove(false)}
      >
        <Dialog.Portal>
          <Dialog.Overlay className="fixed inset-0 z-50 bg-canvas/80 backdrop-blur-sm" />
          <Dialog.Content className="fixed left-1/2 top-1/2 z-50 w-full max-w-md -translate-x-1/2 -translate-y-1/2 rounded-lg border border-default bg-surface shadow-card focus:outline-none">
            <div className="border-b border-subtle px-6 py-4">
              <Dialog.Title className="text-base font-semibold">
                Remove your {providerLabel(status.provider)} key?
              </Dialog.Title>
            </div>
            <div className="px-6 py-5 text-sm text-ink-secondary">
              Accounting-Ops will go back to using Unicorn Commander credits for
              AI in <strong>{workspaceName}</strong>. You can add a key again any
              time. This deletes the stored key permanently.
            </div>
            <div className="flex items-center justify-end gap-2 border-t border-subtle px-6 py-4">
              <Dialog.Close className="rounded-md border border-strong bg-surface px-4 py-2 text-sm text-ink-primary hover:bg-elevated">
                Cancel
              </Dialog.Close>
              <button
                type="button"
                onClick={remove}
                disabled={removing}
                className="inline-flex items-center gap-2 rounded-md bg-debit px-4 py-2 text-sm font-medium text-white transition hover:bg-debit/85 disabled:cursor-not-allowed disabled:opacity-60"
              >
                {removing ? <Loader2 size={14} className="animate-spin" /> : null}
                Remove key
              </button>
            </div>
          </Dialog.Content>
        </Dialog.Portal>
      </Dialog.Root>
    </div>
  );
}

function ComingSoonRow({
  icon,
  title,
  description,
}: {
  icon: React.ReactNode;
  title: string;
  description: string;
}) {
  return (
    <div
      aria-disabled="true"
      className="flex cursor-not-allowed items-start gap-3 rounded-lg border border-subtle bg-white/[0.02] px-4 py-3 opacity-70"
    >
      <span className="mt-0.5 flex h-8 w-8 shrink-0 items-center justify-center rounded-full bg-elevated text-ink-tertiary">
        {icon}
      </span>
      <div className="min-w-0">
        <div className="flex items-center gap-2">
          <p className="text-sm font-medium text-ink-secondary">{title}</p>
          <span className="inline-flex items-center gap-1 rounded-full bg-white/5 px-2 py-0.5 text-[10px] font-medium uppercase tracking-wider text-ink-tertiary">
            <Lock size={9} />
            Soon
          </span>
        </div>
        <p className="mt-1 text-xs leading-relaxed text-ink-tertiary">
          {description}
        </p>
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Save / replace modal
// ---------------------------------------------------------------------------

function KeyModal({
  open,
  onClose,
  workspaceName,
  url,
  onSaved,
}: {
  open: boolean;
  onClose: () => void;
  workspaceName: string;
  url: string;
  onSaved: (s: ByokStatus) => void;
}) {
  const [provider, setProvider] = useState<ByokProvider>("anthropic");
  const [apiKey, setApiKey] = useState("");
  const [baseUrl, setBaseUrl] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [saved, setSaved] = useState(false);

  function reset() {
    setProvider("anthropic");
    setApiKey("");
    setBaseUrl("");
    setSubmitting(false);
    setError(null);
    setSaved(false);
  }

  function close() {
    onClose();
    setTimeout(reset, 200);
  }

  async function submit() {
    if (apiKey.trim().length < 8) {
      setError("Enter a valid API key.");
      return;
    }
    setSubmitting(true);
    setError(null);
    try {
      const body: Record<string, unknown> = {
        provider,
        api_key: apiKey.trim(),
      };
      const trimmedBase = baseUrl.trim();
      if (trimmedBase) body.base_url = trimmedBase;
      const res = await fetch(url, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      });
      if (!res.ok) throw new Error(await readError(res));
      const data = (await res.json()) as ByokStatus;
      onSaved(data);
      setSaved(true);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setSubmitting(false);
    }
  }

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
            <Dialog.Title className="text-base font-semibold">
              {saved ? "Key saved" : "Bring your own API key"}
            </Dialog.Title>
            <Dialog.Close
              onClick={close}
              className="rounded p-1 text-ink-tertiary hover:bg-white/5 hover:text-ink-primary"
            >
              <X size={16} />
            </Dialog.Close>
          </div>

          {saved ? (
            <div className="space-y-4 px-6 py-6">
              <div className="flex items-start gap-3 rounded-md border border-credit/40 bg-credit/[0.06] px-4 py-3">
                <CheckCircle2 size={18} className="mt-0.5 shrink-0 text-credit" />
                <div className="text-sm">
                  <p className="font-semibold text-ink-primary">
                    Saved and encrypted.
                  </p>
                  <p className="mt-1 text-ink-secondary">
                    AI in {workspaceName} now runs on your{" "}
                    {PROVIDER_LABEL[provider]} key, billed by the provider
                    directly. We never display the key again.
                  </p>
                </div>
              </div>
              <button
                type="button"
                onClick={close}
                className="w-full rounded-md bg-brand-500 px-4 py-2 text-sm font-medium text-white hover:bg-brand-600"
              >
                Done
              </button>
            </div>
          ) : (
            <div className="space-y-5 px-6 py-5">
              <Dialog.Description className="text-sm leading-relaxed text-ink-secondary">
                Your usage is billed by the provider directly, not metered by us.
                The key is encrypted at rest and never shown again.
              </Dialog.Description>

              <fieldset>
                <legend className="text-xs font-medium uppercase tracking-wider text-ink-tertiary">
                  Provider
                </legend>
                <div className="mt-2 flex flex-wrap gap-2">
                  {(["anthropic", "openrouter"] as ByokProvider[]).map((p) => (
                    <label
                      key={p}
                      className={cn(
                        "inline-flex cursor-pointer items-center gap-2 rounded-md border px-3 py-1.5 text-sm",
                        provider === p
                          ? "border-brand-500 bg-brand-500/10 text-brand-300"
                          : "border-default bg-canvas hover:border-strong"
                      )}
                    >
                      <input
                        type="radio"
                        name="byok-provider"
                        value={p}
                        checked={provider === p}
                        onChange={() => setProvider(p)}
                        className="sr-only"
                      />
                      {PROVIDER_LABEL[p]}
                    </label>
                  ))}
                </div>
              </fieldset>

              <label className="block">
                <span className="text-xs font-medium uppercase tracking-wider text-ink-tertiary">
                  API key
                </span>
                <input
                  type="password"
                  autoComplete="off"
                  value={apiKey}
                  onChange={(e) => setApiKey(e.target.value)}
                  placeholder={
                    provider === "anthropic" ? "sk-ant-…" : "sk-or-…"
                  }
                  className="mt-1.5 w-full rounded-md border border-default bg-canvas px-3 py-2 font-mono text-xs focus:border-brand-500 focus:outline-none"
                />
              </label>

              <label className="block">
                <span className="text-xs font-medium uppercase tracking-wider text-ink-tertiary">
                  Custom endpoint (optional)
                </span>
                <input
                  type="text"
                  value={baseUrl}
                  onChange={(e) => setBaseUrl(e.target.value)}
                  placeholder="Leave blank for the provider default"
                  maxLength={512}
                  className="mt-1.5 w-full rounded-md border border-default bg-canvas px-3 py-2 font-mono text-xs focus:border-brand-500 focus:outline-none"
                />
              </label>

              {error && (
                <div className="rounded-md border border-debit/40 bg-debit/[0.06] px-3 py-2 text-sm text-debit">
                  {error}
                </div>
              )}

              <div className="flex items-center justify-end gap-2 border-t border-subtle pt-4">
                <button
                  type="button"
                  onClick={close}
                  className="rounded-md border border-strong bg-surface px-4 py-2 text-sm text-ink-primary hover:bg-elevated"
                >
                  Cancel
                </button>
                <button
                  type="button"
                  onClick={submit}
                  disabled={submitting || apiKey.trim().length < 8}
                  className={cn(
                    "inline-flex items-center gap-2 rounded-md bg-brand-500 px-4 py-2 text-sm font-medium text-white shadow-brand-glow transition hover:bg-brand-600",
                    (submitting || apiKey.trim().length < 8) &&
                      "cursor-not-allowed opacity-60"
                  )}
                >
                  {submitting ? (
                    <Loader2 size={14} className="animate-spin" />
                  ) : null}
                  {submitting ? "Saving…" : "Save key"}
                </button>
              </div>
            </div>
          )}
        </Dialog.Content>
      </Dialog.Portal>
    </Dialog.Root>
  );
}
