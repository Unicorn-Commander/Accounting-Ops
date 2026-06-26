"use client";

import Link from "next/link";
import { useState, useTransition } from "react";
import {
  Check,
  ExternalLink,
  Info,
  Loader2,
  PenLine,
  Send,
  Upload,
  X,
} from "lucide-react";
import { Card } from "@/components/primitives";
import { cn } from "@/lib/cn";
import {
  cancelSignatureRequest,
  createSignatureRequest,
  getSignatureRequest,
  markSignedNoFile,
  markSignedUploadUrl,
  type SignatureRequest,
  type SignatureRequestStatus,
} from "@/lib/forms";

// ---------------------------------------------------------------------------
// Status display for a signature request.
// ---------------------------------------------------------------------------

const SR_STATUS_META: Record<
  SignatureRequestStatus,
  { label: string; cls: string }
> = {
  created: { label: "Created", cls: "bg-pending/15 text-pending" },
  sent: { label: "Sent", cls: "bg-pending/15 text-pending" },
  out_for_signature: { label: "Out for signature", cls: "bg-pending/15 text-pending" },
  signed: { label: "Signed", cls: "bg-credit/15 text-credit" },
  declined: { label: "Declined", cls: "bg-debit/15 text-debit" },
  canceled: { label: "Canceled", cls: "bg-white/5 text-ink-tertiary" },
};

const TERMINAL: ReadonlySet<SignatureRequestStatus> = new Set<SignatureRequestStatus>([
  "signed",
  "declined",
  "canceled",
]);

function SrStatusPill({ status }: { status: SignatureRequestStatus }) {
  const meta = SR_STATUS_META[status] ?? {
    label: status,
    cls: "bg-white/5 text-ink-secondary",
  };
  return (
    <span
      className={cn(
        "inline-flex items-center rounded-full px-2 py-0.5 text-[10px] font-medium uppercase tracking-wider",
        meta.cls
      )}
    >
      {meta.label}
    </span>
  );
}

export type EsignPanelProps = {
  workspaceId: string;
  entityId: string;
  wpId: string;
  /** Whether the `esign` channel is enabled for this workspace. */
  esignEnabled: boolean;
  requests: SignatureRequest[];
  /** Re-pull the requests list after any mutation. */
  onChanged: () => void;
};

export function EsignPanel({
  workspaceId,
  entityId,
  wpId,
  esignEnabled,
  requests,
  onChanged,
}: EsignPanelProps) {
  const [signerName, setSignerName] = useState("");
  const [signerEmail, setSignerEmail] = useState("");
  const [subject, setSubject] = useState("");
  const [creating, startCreate] = useTransition();
  const [createError, setCreateError] = useState<string | null>(null);

  function handleCreate(e: React.FormEvent) {
    e.preventDefault();
    if (!signerName.trim() || creating) return;
    setCreateError(null);
    startCreate(async () => {
      try {
        await createSignatureRequest(workspaceId, entityId, wpId, {
          signer_name: signerName.trim(),
          signer_email: signerEmail.trim() || null,
          subject: subject.trim() || null,
        });
        setSignerName("");
        setSignerEmail("");
        setSubject("");
        onChanged();
      } catch (err) {
        setCreateError(
          err instanceof Error ? err.message : "Could not send for signature"
        );
      }
    });
  }

  return (
    <Card padded={false} className="overflow-hidden">
      <div className="flex items-center gap-2 border-b border-subtle px-4 py-3">
        <PenLine size={15} className="shrink-0 text-brand-400" />
        <h3 className="text-sm font-semibold">E-signature</h3>
      </div>

      <div className="space-y-4 p-4">
        {esignEnabled ? (
          <form onSubmit={handleCreate} className="space-y-3">
            <p className="text-[11px] leading-relaxed text-ink-tertiary">
              Send this form for signature. We&apos;ll use your workspace&apos;s
              configured provider; if no provider is connected, a manual request
              is created so you can collect a wet signature and upload it.
            </p>
            <div className="space-y-2.5">
              <label className="block">
                <span className="mb-1 block text-[11px] font-medium text-ink-secondary">
                  Signer name <span className="text-debit">*</span>
                </span>
                <input
                  type="text"
                  required
                  value={signerName}
                  onChange={(e) => setSignerName(e.target.value)}
                  placeholder="e.g. Jane Taxpayer"
                  className="w-full rounded-md border border-default bg-surface px-3 py-1.5 text-sm text-ink-primary placeholder:text-ink-tertiary focus:border-brand-500/60 focus:outline-none focus:ring-2 focus:ring-brand-500/30"
                />
              </label>
              <label className="block">
                <span className="mb-1 block text-[11px] font-medium text-ink-secondary">
                  Signer email{" "}
                  <span className="text-ink-tertiary">(optional)</span>
                </span>
                <input
                  type="email"
                  value={signerEmail}
                  onChange={(e) => setSignerEmail(e.target.value)}
                  placeholder="jane@example.com"
                  className="w-full rounded-md border border-default bg-surface px-3 py-1.5 text-sm text-ink-primary placeholder:text-ink-tertiary focus:border-brand-500/60 focus:outline-none focus:ring-2 focus:ring-brand-500/30"
                />
              </label>
              <label className="block">
                <span className="mb-1 block text-[11px] font-medium text-ink-secondary">
                  Subject <span className="text-ink-tertiary">(optional)</span>
                </span>
                <input
                  type="text"
                  value={subject}
                  onChange={(e) => setSubject(e.target.value)}
                  placeholder="Please sign your tax form"
                  className="w-full rounded-md border border-default bg-surface px-3 py-1.5 text-sm text-ink-primary placeholder:text-ink-tertiary focus:border-brand-500/60 focus:outline-none focus:ring-2 focus:ring-brand-500/30"
                />
              </label>
            </div>
            <div className="flex items-center gap-3">
              <button
                type="submit"
                disabled={!signerName.trim() || creating}
                className={cn(
                  "inline-flex items-center gap-1.5 rounded-md bg-brand-500 px-3 py-1.5 text-xs font-medium text-white transition hover:bg-brand-600 disabled:cursor-not-allowed disabled:bg-elevated disabled:text-ink-disabled",
                  creating && "cursor-wait"
                )}
              >
                {creating ? (
                  <Loader2 size={13} className="animate-spin" />
                ) : (
                  <Send size={13} />
                )}
                Send for signature
              </button>
              {createError && (
                <span className="text-[11px] text-debit" title={createError}>
                  {createError}
                </span>
              )}
            </div>
          </form>
        ) : (
          <div className="flex items-start gap-2 rounded-lg border border-default bg-elevated/50 px-3 py-2.5">
            <Info size={14} className="mt-0.5 shrink-0 text-ink-tertiary" />
            <p className="text-[11px] leading-relaxed text-ink-secondary">
              E-signature isn&apos;t enabled for this workspace. An admin can
              enable it in{" "}
              <Link
                href="/settings/engagement"
                className="text-brand-400 underline-offset-2 hover:underline"
              >
                Engagement settings
              </Link>
              . Any in-flight requests still appear below.
            </p>
          </div>
        )}

        {/* Existing requests — always shown (read-only when disabled). */}
        {requests.length > 0 && (
          <div className="space-y-2">
            <p className="text-[10px] font-medium uppercase tracking-wider text-ink-tertiary">
              Signature requests ({requests.length})
            </p>
            <ul className="space-y-2">
              {requests.map((sr) => (
                <SignatureRequestRow
                  key={sr.id}
                  workspaceId={workspaceId}
                  entityId={entityId}
                  wpId={wpId}
                  request={sr}
                  onChanged={onChanged}
                />
              ))}
            </ul>
          </div>
        )}
      </div>
    </Card>
  );
}

// ---------------------------------------------------------------------------
// A single signature-request row, with status-appropriate actions.
// ---------------------------------------------------------------------------

function SignatureRequestRow({
  workspaceId,
  entityId,
  wpId,
  request,
  onChanged,
}: {
  workspaceId: string;
  entityId: string;
  wpId: string;
  request: SignatureRequest;
  onChanged: () => void;
}) {
  const [busy, setBusy] = useState<null | "refresh" | "mark" | "upload" | "cancel">(
    null
  );
  const [error, setError] = useState<string | null>(null);

  const isTerminal = TERMINAL.has(request.status);
  const isManual = request.provider === "manual";

  async function run<T>(
    action: "refresh" | "mark" | "cancel",
    fn: () => Promise<T>
  ) {
    setBusy(action);
    setError(null);
    try {
      await fn();
      onChanged();
    } catch (e) {
      setError(e instanceof Error ? e.message : "Action failed");
    } finally {
      setBusy(null);
    }
  }

  async function handleUpload(e: React.ChangeEvent<HTMLInputElement>) {
    const file = e.target.files?.[0];
    // Reset the input so the same file can be re-selected after an error.
    e.target.value = "";
    if (!file) return;
    setBusy("upload");
    setError(null);
    try {
      const body = new FormData();
      body.append("signed_pdf", file);
      const res = await fetch(
        markSignedUploadUrl(workspaceId, entityId, wpId, request.id),
        { method: "POST", body }
      );
      if (!res.ok) {
        let detail = `HTTP ${res.status}`;
        try {
          const j = (await res.json()) as { detail?: string; error?: string };
          detail = j.detail ?? j.error ?? detail;
        } catch {
          /* not JSON */
        }
        throw new Error(detail);
      }
      onChanged();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Upload failed");
    } finally {
      setBusy(null);
    }
  }

  return (
    <li className="rounded-md border border-default bg-surface px-3 py-2.5">
      <div className="flex items-start justify-between gap-2">
        <div className="min-w-0">
          <p className="truncate text-xs font-medium text-ink-primary">
            {request.signer_name || "Unnamed signer"}
          </p>
          <p className="mt-0.5 text-[10px] text-ink-tertiary">
            {request.provider === "docuseal"
              ? "Docuseal"
              : isManual
                ? "Manual"
                : request.provider}
            {request.signer_email ? ` · ${request.signer_email}` : ""}
          </p>
        </div>
        <SrStatusPill status={request.status} />
      </div>

      {/* Provider-specific affordances */}
      {request.signing_url && !isTerminal && (
        <a
          href={request.signing_url}
          target="_blank"
          rel="noreferrer"
          className="mt-2 inline-flex items-center gap-1.5 rounded-md border border-default bg-elevated px-2.5 py-1 text-[11px] font-medium text-ink-secondary transition hover:text-ink-primary"
        >
          <ExternalLink size={12} />
          Open signing link
        </a>
      )}
      {isManual && !isTerminal && (
        <p className="mt-2 text-[10px] leading-relaxed text-ink-tertiary">
          Download the form, have it signed, then upload the signed PDF below.
        </p>
      )}

      {/* Actions */}
      {!isTerminal && (
        <div className="mt-2.5 flex flex-wrap items-center gap-1.5">
          <button
            onClick={() =>
              run("refresh", () =>
                getSignatureRequest(workspaceId, entityId, wpId, request.id, true)
              )
            }
            disabled={busy !== null}
            className="inline-flex items-center gap-1 rounded-md border border-default bg-elevated px-2.5 py-1 text-[11px] font-medium text-ink-secondary transition hover:text-ink-primary disabled:cursor-not-allowed disabled:opacity-50"
          >
            {busy === "refresh" ? (
              <Loader2 size={11} className="animate-spin" />
            ) : (
              <Loader2 size={11} />
            )}
            Refresh status
          </button>

          <button
            onClick={() =>
              run("mark", () =>
                markSignedNoFile(workspaceId, entityId, wpId, request.id)
              )
            }
            disabled={busy !== null}
            className="inline-flex items-center gap-1 rounded-md bg-credit/15 px-2.5 py-1 text-[11px] font-medium text-credit transition hover:bg-credit/25 disabled:cursor-not-allowed disabled:opacity-50"
          >
            {busy === "mark" ? (
              <Loader2 size={11} className="animate-spin" />
            ) : (
              <Check size={11} />
            )}
            Mark signed
          </button>

          <label
            className={cn(
              "inline-flex cursor-pointer items-center gap-1 rounded-md border border-default bg-elevated px-2.5 py-1 text-[11px] font-medium text-ink-secondary transition hover:text-ink-primary",
              busy !== null && "cursor-not-allowed opacity-50"
            )}
          >
            {busy === "upload" ? (
              <Loader2 size={11} className="animate-spin" />
            ) : (
              <Upload size={11} />
            )}
            Upload signed PDF
            <input
              type="file"
              accept="application/pdf,.pdf"
              onChange={handleUpload}
              disabled={busy !== null}
              className="hidden"
            />
          </label>

          <button
            onClick={() =>
              run("cancel", () =>
                cancelSignatureRequest(workspaceId, entityId, wpId, request.id)
              )
            }
            disabled={busy !== null}
            className="inline-flex items-center gap-1 rounded-md border border-default bg-surface px-2.5 py-1 text-[11px] font-medium text-ink-tertiary transition hover:border-debit/40 hover:text-debit disabled:cursor-not-allowed disabled:opacity-50"
          >
            {busy === "cancel" ? (
              <Loader2 size={11} className="animate-spin" />
            ) : (
              <X size={11} />
            )}
            Cancel
          </button>
        </div>
      )}

      {error && (
        <p className="mt-2 text-[10px] text-debit" title={error}>
          {error}
        </p>
      )}
    </li>
  );
}
