"use client";

import Link from "next/link";
import { useCallback, useEffect, useState, useTransition } from "react";
import {
  AlertTriangle,
  Download,
  FileText,
  Loader2,
} from "lucide-react";
import { Card } from "@/components/primitives";
import { cn } from "@/lib/cn";
import {
  attestWorkProduct,
  downloadFrom,
  getEngagementPolicy,
  getWorkProduct,
  listSignatureRequests,
  pdfUrl,
  STATUS_LABEL,
  STATUS_TONE,
  type EngagementPolicy,
  type SignatureRequest,
  type WorkProductDetail,
  type WorkProductStatus,
} from "@/lib/forms";
import { AttestCard } from "../_components/attest-card";
import { EsignPanel } from "../_components/esign-panel";
import { EfilePanel } from "../_components/efile-panel";

function titleFor(wp: WorkProductDetail): string {
  return wp.title || `${wp.kind.toUpperCase()} ${wp.tax_year ?? ""}`.trim();
}

function StatusPill({ status }: { status: WorkProductStatus }) {
  const label = STATUS_LABEL[status] ?? status;
  const tone = STATUS_TONE[status] ?? "bg-white/5 text-ink-secondary";
  return (
    <span
      className={cn(
        "inline-flex items-center rounded-full px-2 py-0.5 text-[10px] font-medium uppercase tracking-wider",
        tone
      )}
    >
      {label}
    </span>
  );
}

function statusBlurb(status: WorkProductStatus): string {
  switch (status) {
    case "draft":
      return "Draft — carries a DRAFT watermark until certified.";
    case "self_approved":
      return "Self-certified by you.";
    case "pro_reviewed":
      return "Reviewed by a licensed professional.";
    case "finalized":
      return "Finalized.";
    default:
      return "";
  }
}

export type WpDetailClientProps = {
  workspaceId: string;
  entityId: string;
  entitySlug: string | null;
  wpId: string;
};

export function WpDetailClient({
  workspaceId,
  entityId,
  entitySlug,
  wpId,
}: WpDetailClientProps) {
  const [wp, setWp] = useState<WorkProductDetail | null>(null);
  const [policy, setPolicy] = useState<EngagementPolicy | null>(null);
  const [requests, setRequests] = useState<SignatureRequest[]>([]);
  const [loading, setLoading] = useState(true);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [notFound, setNotFound] = useState(false);

  const [attestPending, startAttest] = useTransition();
  const [downloadError, setDownloadError] = useState<string | null>(null);
  const [downloading, setDownloading] = useState(false);

  // Bust the iframe cache after an attest so the watermark refreshes.
  const [pdfNonce, setPdfNonce] = useState(0);

  const loadAll = useCallback(async () => {
    setLoading(true);
    setLoadError(null);
    setNotFound(false);
    try {
      // Work product is required; policy + signature requests are best-effort
      // so a transient failure there doesn't blank the whole screen.
      const [wpRes, policyRes, srRes] = await Promise.allSettled([
        getWorkProduct(workspaceId, entityId, wpId),
        getEngagementPolicy(workspaceId),
        listSignatureRequests(workspaceId, entityId, wpId),
      ]);

      if (wpRes.status === "rejected") {
        const msg =
          wpRes.reason instanceof Error
            ? wpRes.reason.message
            : String(wpRes.reason);
        if (/404|not found/i.test(msg)) {
          setNotFound(true);
          return;
        }
        throw new Error(msg);
      }
      setWp(wpRes.value);

      setPolicy(policyRes.status === "fulfilled" ? policyRes.value : null);
      setRequests(srRes.status === "fulfilled" ? srRes.value.items : []);
    } catch (e) {
      setLoadError(e instanceof Error ? e.message : String(e));
    } finally {
      setLoading(false);
    }
  }, [workspaceId, entityId, wpId]);

  useEffect(() => {
    void loadAll();
  }, [loadAll]);

  // Re-pull just the signature requests after an e-sign mutation.
  const refreshRequests = useCallback(() => {
    void listSignatureRequests(workspaceId, entityId, wpId)
      .then((r) => setRequests(r.items))
      .catch(() => {
        /* leave the prior list in place on a transient failure */
      });
  }, [workspaceId, entityId, wpId]);

  const bumpNonce = useCallback(() => setPdfNonce((n) => n + 1), []);

  function handleAttest(): Promise<void> {
    return new Promise<void>((resolve, reject) => {
      startAttest(async () => {
        try {
          const res = await attestWorkProduct(workspaceId, entityId, wpId);
          setWp((prev) =>
            prev
              ? {
                  ...prev,
                  status: res.status,
                  attestation_kind: res.attestation_kind,
                }
              : prev
          );
          bumpNonce();
          resolve();
        } catch (e) {
          reject(e instanceof Error ? e : new Error(String(e)));
        }
      });
    });
  }

  async function handleDownload() {
    if (!wp) return;
    setDownloadError(null);
    setDownloading(true);
    try {
      const filename = `${wp.kind}-${wp.tax_year ?? ""}-${
        entitySlug ?? "entity"
      }.pdf`.replace(/-+/g, "-");
      await downloadFrom(
        pdfUrl(workspaceId, entityId, wpId, "attachment"),
        filename
      );
    } catch (e) {
      setDownloadError(e instanceof Error ? e.message : "Download failed");
    } finally {
      setDownloading(false);
    }
  }

  // ----- Loading -----------------------------------------------------------
  if (loading) {
    return (
      <div className="flex items-center gap-2 rounded-lg border border-default bg-surface px-5 py-8 text-sm text-ink-tertiary">
        <Loader2 size={15} className="animate-spin" />
        Loading this work product…
      </div>
    );
  }

  // ----- Not found ---------------------------------------------------------
  if (notFound) {
    return (
      <Card>
        <div className="mx-auto flex max-w-md flex-col items-center gap-3 py-6 text-center">
          <span className="flex h-12 w-12 items-center justify-center rounded-full bg-elevated text-ink-disabled">
            <FileText size={26} aria-hidden="true" />
          </span>
          <p className="text-base font-medium text-ink-primary">
            Work product not found
          </p>
          <p className="text-xs leading-relaxed text-ink-tertiary">
            This form doesn&apos;t exist for the selected entity, or it may
            belong to a different entity. Go back and pick it from the list.
          </p>
          <Link
            href="/taxes/work-products"
            className="mt-1 inline-flex items-center gap-1.5 rounded-md bg-brand-500 px-3 py-1.5 text-xs font-medium text-white transition hover:bg-brand-600"
          >
            All work products
          </Link>
        </div>
      </Card>
    );
  }

  // ----- Hard load error ---------------------------------------------------
  if (loadError || !wp) {
    return (
      <div className="flex items-start gap-2 rounded-md border border-debit/40 bg-debit/[0.07] px-4 py-3">
        <AlertTriangle size={14} className="mt-0.5 shrink-0 text-debit" />
        <div className="min-w-0">
          <p className="text-sm font-medium text-debit">
            Couldn&apos;t load this work product
          </p>
          {loadError && (
            <p className="mt-0.5 text-[11px] text-debit/80">{loadError}</p>
          )}
          <button
            onClick={() => void loadAll()}
            className="mt-2 inline-flex items-center gap-1.5 rounded-md border border-default bg-elevated px-3 py-1.5 text-xs font-medium text-ink-secondary hover:text-ink-primary"
          >
            <Loader2 size={12} /> Try again
          </button>
        </div>
      </div>
    );
  }

  const inputs = wp.inputs ? Object.entries(wp.inputs) : [];

  const canSelfAttest =
    policy?.review_mode === "self_attest_allowed" && wp.status === "draft";
  const proRequired = policy?.review_mode === "pro_required";
  const esignEnabled = policy?.submission_channels?.esign === true;
  const is1040 = wp.kind === "1040";

  return (
    <div className="grid grid-cols-1 gap-6 lg:grid-cols-[1fr_360px]">
      {/* LEFT — PDF preview + what's on this form */}
      <div className="space-y-4">
        <Card padded={false} className="overflow-hidden">
          <div className="flex items-center justify-between gap-3 border-b border-subtle px-4 py-3">
            <div className="min-w-0">
              <div className="flex items-center gap-2">
                <h2 className="truncate text-sm font-semibold">{titleFor(wp)}</h2>
                <StatusPill status={wp.status} />
              </div>
              <p className="mt-0.5 text-[11px] text-ink-tertiary">
                {statusBlurb(wp.status)}
              </p>
            </div>
            <button
              onClick={handleDownload}
              disabled={downloading}
              className="inline-flex shrink-0 items-center gap-1.5 rounded-md bg-brand-500 px-3 py-1.5 text-xs font-medium text-white transition hover:bg-brand-600 disabled:cursor-not-allowed disabled:bg-elevated disabled:text-ink-disabled"
            >
              {downloading ? (
                <Loader2 size={13} className="animate-spin" />
              ) : (
                <Download size={13} />
              )}
              {downloading ? "Preparing…" : "Download"}
            </button>
          </div>
          {downloadError && (
            <div className="border-b border-subtle bg-debit/[0.06] px-4 py-2 text-[11px] text-debit">
              {downloadError}
            </div>
          )}
          <iframe
            key={`${wp.id}-${pdfNonce}`}
            src={`${pdfUrl(workspaceId, entityId, wpId, "inline")}#${pdfNonce}`}
            title={`${titleFor(wp)} preview`}
            className="h-[640px] w-full bg-canvas"
          />
        </Card>

        {/* What's on this form — the stored inputs + their source refs. */}
        <Card padded={false} className="overflow-hidden">
          <div className="border-b border-subtle px-4 py-3">
            <h3 className="text-sm font-semibold">What&apos;s on this form</h3>
            <p className="mt-0.5 text-[11px] text-ink-tertiary">
              Every figure shows where it came from — nothing is fabricated.
            </p>
          </div>
          {inputs.length === 0 ? (
            <div className="px-4 py-6 text-center text-xs text-ink-tertiary">
              No stored inputs for this form.
            </div>
          ) : (
            <ul className="divide-y divide-subtle/60">
              {inputs.map(([key, field]) => (
                <li key={key} className="px-4 py-2.5">
                  <div className="flex items-baseline justify-between gap-3">
                    <span className="shrink-0 text-[11px] text-ink-tertiary">
                      {key}
                    </span>
                    <span className="truncate text-right text-sm font-medium text-ink-primary">
                      {field.value || "—"}
                    </span>
                  </div>
                  {field.source_ref && (
                    <p className="mt-0.5 text-right text-[10px] text-ink-tertiary">
                      source: {field.source_ref}
                    </p>
                  )}
                </li>
              ))}
            </ul>
          )}
        </Card>
      </div>

      {/* RIGHT — action panels */}
      <div className="space-y-4">
        <AttestCard
          onAttest={handleAttest}
          pending={attestPending}
          canSelfAttest={canSelfAttest}
          proRequired={proRequired}
          status={wp.status}
        />

        <EsignPanel
          workspaceId={workspaceId}
          entityId={entityId}
          wpId={wpId}
          esignEnabled={esignEnabled}
          requests={requests}
          onChanged={refreshRequests}
        />

        {is1040 ? (
          <EfilePanel
            workspaceId={workspaceId}
            entityId={entityId}
            wpId={wpId}
            taxYear={wp.tax_year}
          />
        ) : (
          <div className="flex items-start gap-2 rounded-lg border border-default bg-elevated/50 px-4 py-3">
            <FileText size={14} className="mt-0.5 shrink-0 text-ink-tertiary" />
            <p className="text-[11px] leading-relaxed text-ink-secondary">
              E-file packaging currently supports Form 1040.
            </p>
          </div>
        )}
      </div>
    </div>
  );
}
