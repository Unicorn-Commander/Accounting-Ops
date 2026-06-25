"use client";

import Link from "next/link";
import { useRouter } from "next/navigation";
import { useCallback, useEffect, useMemo, useState, useTransition } from "react";
import {
  AlertTriangle,
  Check,
  Download,
  FileText,
  Info,
  Loader2,
  MessageSquare,
  ShieldCheck,
} from "lucide-react";
import { Card } from "@/components/primitives";
import { cn } from "@/lib/cn";

// ---------------------------------------------------------------------------
// Types — shape of the backend work-product list rows + engagement policy.
// ---------------------------------------------------------------------------

type WorkProductStatus =
  | "draft"
  | "self_approved"
  | "pro_reviewed"
  | "finalized";

type WorkProduct = {
  id: string;
  kind: string;
  tax_year: number | null;
  title: string | null;
  status: WorkProductStatus;
  attestation_kind: string | null;
  attested_by_uc_uid?: string | null;
  attested_at?: string | null;
  created_at: string | null;
  updated_at?: string | null;
};

type ReviewMode = "pro_required" | "self_attest_allowed" | "autonomous_off";

type EngagementPolicy = {
  review_mode: ReviewMode;
  has_licensed_pro: boolean;
};

// ---------------------------------------------------------------------------
// Status badge — mirrors StatusChip styling (rounded-full pill, tone colors).
// ---------------------------------------------------------------------------

const STATUS_META: Record<
  WorkProductStatus,
  { label: string; cls: string }
> = {
  draft: { label: "Draft", cls: "bg-pending/15 text-pending" },
  self_approved: { label: "Self-certified", cls: "bg-credit/15 text-credit" },
  pro_reviewed: { label: "Pro reviewed", cls: "bg-credit/15 text-credit" },
  finalized: { label: "Finalized", cls: "bg-credit/15 text-credit" },
};

function StatusBadge({ status }: { status: WorkProductStatus }) {
  const meta = STATUS_META[status] ?? {
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

function fmtDate(s: string | null | undefined): string {
  if (!s) return "—";
  try {
    return new Date(s).toLocaleDateString(undefined, {
      year: "numeric",
      month: "short",
      day: "numeric",
    });
  } catch {
    return s;
  }
}

export type W4pClientProps = {
  workspaceId: string;
  workspaceSlug: string;
  entityId: string;
  entitySlug: string | null;
};

export function W4pClient({
  workspaceId,
  workspaceSlug,
  entityId,
  entitySlug,
}: W4pClientProps) {
  const router = useRouter();
  const [pending, startTransition] = useTransition();

  const [items, setItems] = useState<WorkProduct[]>([]);
  const [policy, setPolicy] = useState<EngagementPolicy | null>(null);
  const [loading, setLoading] = useState(true);
  const [loadError, setLoadError] = useState<string | null>(null);

  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [confirmChecked, setConfirmChecked] = useState(false);
  const [attestError, setAttestError] = useState<string | null>(null);
  const [downloadError, setDownloadError] = useState<string | null>(null);
  const [downloading, setDownloading] = useState(false);

  // Bust the iframe / blob cache after an attest so the watermark refreshes.
  const [pdfNonce, setPdfNonce] = useState(0);

  // Base for the metadata + policy calls. NOTE: the generic auth-injecting
  // proxy at /api/proxy/[...path] forwards everything under /api/* on the
  // backend, so the caller path must itself START with `api` — i.e.
  // /api/proxy/api/workspaces/... maps to /api/workspaces/... on the backend.
  const wpBase = `/api/proxy/api/workspaces/${encodeURIComponent(
    workspaceId
  )}/entities/${encodeURIComponent(entityId)}/work-products`;

  // The filled PDF is binary, so it goes through a DEDICATED streaming route
  // (the generic proxy would corrupt the bytes via res.text()). This route
  // lives at /api/workspaces/{wid}/entities/{eid}/work-products/{id}/document.pdf
  // and is NOT under /api/proxy.
  const pdfUrl = useCallback(
    (wpId: string, disposition: "inline" | "attachment") =>
      `/api/workspaces/${encodeURIComponent(
        workspaceId
      )}/entities/${encodeURIComponent(
        entityId
      )}/work-products/${encodeURIComponent(
        wpId
      )}/document.pdf?disposition=${disposition}`,
    [workspaceId, entityId]
  );

  const loadAll = useCallback(async () => {
    setLoading(true);
    setLoadError(null);
    try {
      const [wpRes, policyRes] = await Promise.all([
        fetch(`${wpBase}?kind=w-4p`, { cache: "no-store" }),
        fetch(
          `/api/proxy/api/workspaces/${encodeURIComponent(
            workspaceId
          )}/engagement/policy`,
          { cache: "no-store" }
        ),
      ]);

      if (!wpRes.ok) {
        const body = await wpRes.text().catch(() => "");
        throw new Error(
          `Could not load W-4P documents (HTTP ${wpRes.status}${
            body ? ": " + body : ""
          })`
        );
      }
      const wpJson = (await wpRes.json()) as { items?: WorkProduct[] };
      const list = (wpJson.items ?? []).filter((i) => i.kind === "w-4p");
      setItems(list);

      // Default-select the most recent (list is ordered created_at DESC).
      setSelectedId((prev) =>
        prev && list.some((i) => i.id === prev) ? prev : list[0]?.id ?? null
      );

      // Policy is best-effort — if it fails we simply hide the self-certify
      // affordance rather than block the whole page.
      if (policyRes.ok) {
        const p = (await policyRes.json()) as EngagementPolicy;
        setPolicy(p);
      } else {
        setPolicy(null);
      }
    } catch (e) {
      setLoadError(e instanceof Error ? e.message : String(e));
    } finally {
      setLoading(false);
    }
  }, [wpBase, workspaceId]);

  useEffect(() => {
    void loadAll();
  }, [loadAll]);

  const selected = useMemo(
    () => items.find((i) => i.id === selectedId) ?? null,
    [items, selectedId]
  );

  // Reset the per-document UI state whenever the selection changes.
  useEffect(() => {
    setConfirmChecked(false);
    setAttestError(null);
    setDownloadError(null);
  }, [selectedId]);

  const canSelfAttest =
    policy?.review_mode === "self_attest_allowed" &&
    selected?.status === "draft";
  const proRequired = policy?.review_mode === "pro_required";

  async function handleDownload() {
    if (!selected) return;
    setDownloadError(null);
    setDownloading(true);
    try {
      const res = await fetch(pdfUrl(selected.id, "attachment"), {
        cache: "no-store",
      });
      if (!res.ok) {
        let detail = `HTTP ${res.status}`;
        try {
          const json = await res.json();
          detail = json.error ?? json.detail ?? detail;
        } catch {
          // not JSON
        }
        throw new Error(detail);
      }
      // Filename: prefer Content-Disposition, else build a friendly one.
      let filename = `w-4p-${selected.tax_year ?? ""}-${
        entitySlug ?? "entity"
      }.pdf`.replace(/-+/g, "-");
      const cd = res.headers.get("content-disposition");
      if (cd) {
        const match = cd.match(/filename="?([^";\n]+)"?/i);
        if (match) filename = match[1];
      }
      const blob = await res.blob();
      const url = URL.createObjectURL(blob);
      const a = document.createElement("a");
      a.href = url;
      a.download = filename;
      document.body.appendChild(a);
      a.click();
      document.body.removeChild(a);
      URL.revokeObjectURL(url);
    } catch (e) {
      setDownloadError(e instanceof Error ? e.message : "Download failed");
    } finally {
      setDownloading(false);
    }
  }

  function handleAttest() {
    if (!selected || !confirmChecked) return;
    setAttestError(null);
    startTransition(async () => {
      try {
        const res = await fetch(`${wpBase}/${selected.id}/attest`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ confirm: true }),
        });
        const body = (await res.json().catch(() => ({}))) as {
          ok?: boolean;
          status?: WorkProductStatus;
          detail?: string;
          error?: string;
        };
        if (!res.ok) {
          throw new Error(body.detail ?? body.error ?? `HTTP ${res.status}`);
        }
        // Optimistically flip the row's status, then re-render the PDF so the
        // watermark updates (draft → self-certified).
        const nextStatus = body.status ?? "self_approved";
        setItems((prev) =>
          prev.map((i) =>
            i.id === selected.id
              ? { ...i, status: nextStatus, attestation_kind: "self" }
              : i
          )
        );
        setConfirmChecked(false);
        setPdfNonce((n) => n + 1);
        // Pull authoritative state in the background.
        void loadAll();
      } catch (e) {
        setAttestError(e instanceof Error ? e.message : "Could not self-certify");
      }
    });
  }

  // ----- Loading -----------------------------------------------------------
  if (loading) {
    return (
      <div className="flex items-center gap-2 rounded-lg border border-default bg-surface px-5 py-8 text-sm text-ink-tertiary">
        <Loader2 size={15} className="animate-spin" />
        Loading your W-4P documents…
      </div>
    );
  }

  // ----- Hard load error ---------------------------------------------------
  if (loadError) {
    return (
      <div className="space-y-4">
        <LastMileGuidance />
        <div className="flex items-start gap-2 rounded-md border border-debit/40 bg-debit/[0.07] px-4 py-3">
          <AlertTriangle size={14} className="mt-0.5 shrink-0 text-debit" />
          <div className="min-w-0">
            <p className="text-sm font-medium text-debit">
              Couldn&apos;t load W-4P documents
            </p>
            <p className="mt-0.5 text-[11px] text-debit/80">{loadError}</p>
            <button
              onClick={() => void loadAll()}
              className="mt-2 inline-flex items-center gap-1.5 rounded-md border border-default bg-elevated px-3 py-1.5 text-xs font-medium text-ink-secondary hover:text-ink-primary"
            >
              <Loader2 size={12} /> Try again
            </button>
          </div>
        </div>
      </div>
    );
  }

  // ----- Empty state -------------------------------------------------------
  if (items.length === 0) {
    return (
      <div className="space-y-5">
        <LastMileGuidance />
        <Card>
          <div className="mx-auto flex max-w-md flex-col items-center gap-3 py-6 text-center">
            <span className="flex h-12 w-12 items-center justify-center rounded-full bg-elevated text-ink-disabled">
              <FileText size={26} aria-hidden="true" />
            </span>
            <p className="text-base font-medium text-ink-primary">
              No W-4P prepared yet
            </p>
            <p className="text-xs leading-relaxed text-ink-tertiary">
              The Personal 1040 Specialist can fill out a Form W-4P for you —
              ask for it in Chat (e.g. &ldquo;prepare my W-4P pension
              withholding&rdquo;). Once it&apos;s ready it will show up here to
              review, self-certify, and download.
            </p>
            <Link
              href="/chat"
              className="mt-1 inline-flex items-center gap-1.5 rounded-md bg-brand-500 px-3 py-1.5 text-xs font-medium text-white transition hover:bg-brand-600"
            >
              <MessageSquare size={13} />
              Open Chat
            </Link>
          </div>
        </Card>
      </div>
    );
  }

  // ----- Main: list + preview + certify -----------------------------------
  return (
    <div className="space-y-6">
      <LastMileGuidance />

      <div className="grid grid-cols-1 gap-6 lg:grid-cols-[300px_1fr]">
        {/* Document list */}
        <Card padded={false} className="overflow-hidden">
          <div className="border-b border-subtle px-4 py-3">
            <h2 className="text-sm font-semibold">Your W-4P documents</h2>
            <p className="mt-0.5 text-[11px] text-ink-tertiary">
              {items.length} {items.length === 1 ? "document" : "documents"}
            </p>
          </div>
          <ul className="divide-y divide-subtle/60">
            {items.map((wp) => {
              const active = wp.id === selectedId;
              return (
                <li key={wp.id}>
                  <button
                    onClick={() => setSelectedId(wp.id)}
                    className={cn(
                      "flex w-full flex-col items-start gap-1.5 px-4 py-3 text-left transition",
                      active
                        ? "bg-brand-500/[0.08]"
                        : "hover:bg-elevated/60"
                    )}
                  >
                    <div className="flex w-full items-center justify-between gap-2">
                      <span
                        className={cn(
                          "truncate text-sm font-medium",
                          active ? "text-brand-300" : "text-ink-primary"
                        )}
                      >
                        {wp.title || `Form W-4P ${wp.tax_year ?? ""}`.trim()}
                      </span>
                      <StatusBadge status={wp.status} />
                    </div>
                    <span className="text-[10px] text-ink-tertiary">
                      Tax year {wp.tax_year ?? "—"} · created{" "}
                      {fmtDate(wp.created_at)}
                    </span>
                  </button>
                </li>
              );
            })}
          </ul>
        </Card>

        {/* Selected document: preview + actions */}
        {selected && (
          <div className="space-y-4">
            {/* PDF preview */}
            <Card padded={false} className="overflow-hidden">
              <div className="flex items-center justify-between gap-3 border-b border-subtle px-4 py-3">
                <div className="min-w-0">
                  <h3 className="truncate text-sm font-semibold">
                    {selected.title ||
                      `Form W-4P ${selected.tax_year ?? ""}`.trim()}
                  </h3>
                  <p className="mt-0.5 text-[11px] text-ink-tertiary">
                    {selected.status === "draft"
                      ? "Draft — carries a DRAFT watermark until certified."
                      : selected.status === "self_approved"
                        ? "Self-certified by you."
                        : selected.status === "pro_reviewed"
                          ? "Reviewed by a licensed professional."
                          : "Finalized."}
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
                key={`${selected.id}-${pdfNonce}`}
                src={`${pdfUrl(selected.id, "inline")}#${pdfNonce}`}
                title={`Form W-4P ${selected.tax_year ?? ""} preview`}
                className="h-[640px] w-full bg-canvas"
              />
            </Card>

            {/* Self-certify card — only when policy allows AND status is draft */}
            {canSelfAttest && (
              <Card>
                <div className="flex items-start gap-3">
                  <div className="flex h-9 w-9 shrink-0 items-center justify-center rounded-md bg-elevated text-brand-400 ring-1 ring-inset ring-brand-500/30">
                    <ShieldCheck size={18} />
                  </div>
                  <div className="min-w-0 flex-1">
                    <h3 className="text-sm font-semibold">
                      Self-certify this W-4P
                    </h3>
                    <p className="mt-1 text-xs leading-relaxed text-ink-secondary">
                      No licensed professional is required for your workspace,
                      so you can certify this form yourself. Certifying removes
                      the DRAFT watermark — it does <strong>not</strong> file
                      anything. You still need to sign page 1 and give it to your
                      pension payer.
                    </p>

                    <label className="mt-4 flex cursor-pointer items-start gap-2.5">
                      <input
                        type="checkbox"
                        checked={confirmChecked}
                        onChange={(e) => setConfirmChecked(e.target.checked)}
                        className="mt-0.5 h-4 w-4 shrink-0 rounded border-default bg-elevated accent-brand-500"
                      />
                      <span className="text-xs leading-relaxed text-ink-secondary">
                        I have reviewed every line of this W-4P, entered my
                        Social Security number, and confirm the information is
                        correct to the best of my knowledge.
                      </span>
                    </label>

                    <div className="mt-4 flex items-center gap-3">
                      <button
                        onClick={handleAttest}
                        disabled={!confirmChecked || pending}
                        className={cn(
                          "inline-flex items-center gap-1.5 rounded-md bg-credit/15 px-3 py-1.5 text-xs font-medium text-credit transition hover:bg-credit/25 disabled:cursor-not-allowed disabled:opacity-50",
                          pending && "cursor-wait"
                        )}
                      >
                        {pending ? (
                          <Loader2 size={13} className="animate-spin" />
                        ) : (
                          <Check size={13} />
                        )}
                        Self-certify
                      </button>
                      {attestError && (
                        <span className="text-[11px] text-debit" title={attestError}>
                          {attestError}
                        </span>
                      )}
                    </div>
                  </div>
                </div>
              </Card>
            )}

            {/* Already certified — calm confirmation */}
            {selected.status !== "draft" && (
              <div className="flex items-start gap-2 rounded-lg border border-credit/30 bg-credit/[0.06] px-4 py-3">
                <Check size={14} className="mt-0.5 shrink-0 text-credit" />
                <p className="text-[11px] leading-relaxed text-credit">
                  {selected.status === "self_approved"
                    ? "You self-certified this W-4P — the DRAFT watermark is removed. Print page 1, sign Step 5, and give it to your pension payer."
                    : "This W-4P has been reviewed. Print page 1, sign Step 5, and give it to your pension payer."}
                </p>
              </div>
            )}

            {/* pro_required gate — calm note, NOT an error */}
            {proRequired && selected.status === "draft" && (
              <div className="flex items-start gap-2 rounded-lg border border-default bg-elevated/50 px-4 py-3">
                <Info size={14} className="mt-0.5 shrink-0 text-ink-tertiary" />
                <p className="text-[11px] leading-relaxed text-ink-secondary">
                  A licensed professional review is required before this can be
                  finalized. If no professional is available, an admin can enable
                  self-certification in Settings.
                </p>
              </div>
            )}
          </div>
        )}
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Plain-language last-mile guidance — ALWAYS visible for a W-4P.
// ---------------------------------------------------------------------------

function LastMileGuidance() {
  return (
    <div className="flex items-start gap-3 rounded-lg border border-brand-500/30 bg-brand-500/[0.06] px-4 py-3.5">
      <Info size={16} className="mt-0.5 shrink-0 text-brand-400" />
      <div className="min-w-0 text-xs leading-relaxed text-ink-secondary">
        <p className="font-semibold text-ink-primary">
          This form goes to your pension payer — never to the IRS.
        </p>
        <p className="mt-1">
          Review every line, enter your Social Security number, then{" "}
          <strong>print page 1 and sign Step 5</strong>. Give the signed page 1
          to whoever pays your pension (your plan administrator or payer). You
          do <strong>not</strong> file a W-4P with the IRS.
        </p>
      </div>
    </div>
  );
}
