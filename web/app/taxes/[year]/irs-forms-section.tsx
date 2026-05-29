"use client";

/**
 * IrsFormsSection — "IRS Forms" card in the /taxes/[year] page.
 *
 * Shows Download + View buttons for:
 *   - Form 1120 (income / deductions pre-filled)
 *   - Schedule L (balance sheet pre-filled)
 *   - Form 1125-E (officer compensation pre-filled, if relevant)
 *   - "Download all (zip)" — the 1120-package bundle
 *
 * Downloads are browser-initiated fetches via the web-layer proxy at
 *   /api/workspaces/{wid}/entities/{eid}/irs-forms/{year}/{form}.pdf
 * The proxy streams the filled PDF (or zip) back from app:8000 with
 * Content-Disposition: attachment so the browser triggers a save dialog.
 *
 * "View" opens the PDF in a new browser tab using ?disposition=inline on the
 * same proxy URL. The zip package has no View button (browsers can't display
 * zip files inline).
 *
 * Every form carries a DRAFT watermark — the UI surfaces this reminder.
 */

import { useState } from "react";
import { Download, Eye, FileText, Package, AlertTriangle } from "lucide-react";
import { cn } from "@/lib/cn";

type IrsFormsDef = {
  key: string;
  label: string;
  description: string;
  icon: React.ReactNode;
  isZip?: boolean;
};

const FORMS: IrsFormsDef[] = [
  {
    key: "1120",
    label: "Form 1120",
    description: "U.S. Corporation Income Tax Return — income & deductions pre-filled",
    icon: <FileText size={14} />,
  },
  {
    key: "1120-schedule-l",
    label: "Schedule L",
    description: "Balance Sheet per Books — balance sheet lines pre-filled",
    icon: <FileText size={14} />,
  },
  {
    key: "1125-e",
    label: "Form 1125-E",
    description: "Compensation of Officers — officer rows pre-filled from queue",
    icon: <FileText size={14} />,
  },
  {
    key: "1120-package",
    label: "Download all (zip)",
    description: "Form 1120 + Schedule L + Form 1125-E bundled as a zip archive",
    icon: <Package size={14} />,
    isZip: true,
  },
];

type Props = {
  workspaceId: string;
  entityId: string;
  year: number;
};

export function IrsFormsSection({ workspaceId, entityId, year }: Props) {
  const [busy, setBusy] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  // Build the proxy URL for a form (no disposition — default attachment).
  function formUrl(formKey: string): string {
    return (
      `/api/workspaces/${encodeURIComponent(workspaceId)}` +
      `/entities/${encodeURIComponent(entityId)}` +
      `/irs-forms/${encodeURIComponent(year)}` +
      `/${encodeURIComponent(formKey)}.pdf`
    );
  }

  async function handleDownload(formKey: string, isZip?: boolean) {
    setError(null);
    setBusy(formKey);
    try {
      const url = formUrl(formKey);
      const res = await fetch(url, { cache: "no-store" });

      if (!res.ok) {
        let detail = `HTTP ${res.status}`;
        try {
          const json = await res.json();
          detail = json.error ?? json.detail ?? detail;
        } catch {
          // not JSON
        }
        setError(detail);
        return;
      }

      // Determine filename from Content-Disposition or build one
      let filename = `irs-form-${formKey}-${year}${isZip ? ".zip" : ".pdf"}`;
      const cd = res.headers.get("content-disposition");
      if (cd) {
        const match = cd.match(/filename="?([^";\n]+)"?/i);
        if (match) filename = match[1];
      }

      const blob = await res.blob();
      const objectUrl = URL.createObjectURL(blob);
      const a = document.createElement("a");
      a.href = objectUrl;
      a.download = filename;
      document.body.appendChild(a);
      a.click();
      document.body.removeChild(a);
      URL.revokeObjectURL(objectUrl);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Download failed");
    } finally {
      setBusy(null);
    }
  }

  function handleView(formKey: string) {
    // Open the PDF inline in a new tab via ?disposition=inline.
    const url = formUrl(formKey) + "?disposition=inline";
    window.open(url, "_blank", "noopener,noreferrer");
  }

  return (
    <div className="space-y-4">
      {/* DRAFT notice */}
      <div className="flex items-start gap-2 rounded-lg border border-pending/40 bg-pending/[0.07] px-3 py-2.5">
        <AlertTriangle size={14} className="mt-0.5 shrink-0 text-pending" />
        <p className="text-[11px] text-pending leading-relaxed">
          <span className="font-semibold">DRAFT forms only.</span>{" "}
          Every PDF carries a diagonal DRAFT watermark and a "do not file" banner
          on page 1. The CPA must review, complete remaining lines (tax computation,
          NOL, estimated payments), sign, and submit. Do not file these forms as-is.
        </p>
      </div>

      {/* Form rows — each has a View button (PDF only) + Download button */}
      <div className="flex flex-col gap-2">
        {FORMS.map((form) => {
          const isBusy = busy === form.key;
          return (
            <div
              key={form.key}
              className={cn(
                "flex w-full items-center gap-3 rounded-lg border px-3 py-3",
                form.isZip
                  ? "border-brand-500/50 bg-brand-500/[0.06]"
                  : "border-default bg-surface"
              )}
            >
              {/* Icon */}
              <span
                className={cn(
                  "flex h-7 w-7 shrink-0 items-center justify-center rounded-md border",
                  form.isZip
                    ? "border-brand-500/40 bg-brand-500/15 text-brand-300"
                    : "border-subtle bg-elevated text-ink-secondary"
                )}
              >
                {isBusy ? (
                  <span className="inline-block h-3 w-3 animate-spin rounded-full border border-current border-t-transparent" />
                ) : (
                  form.icon
                )}
              </span>

              {/* Label + description */}
              <div className="min-w-0 flex-1">
                <p
                  className={cn(
                    "text-xs font-medium leading-snug",
                    form.isZip ? "text-brand-300" : "text-ink-primary"
                  )}
                >
                  {isBusy ? "Generating…" : form.label}
                </p>
                <p className="mt-0.5 text-[10px] text-ink-tertiary leading-snug">
                  {form.description}
                </p>
              </div>

              {/* Action buttons */}
              <div className="flex shrink-0 items-center gap-1.5">
                {/* "View" — PDF only (zip can't be rendered inline) */}
                {!form.isZip && (
                  <button
                    type="button"
                    onClick={() => handleView(form.key)}
                    disabled={isBusy}
                    title="Open in browser tab"
                    className="inline-flex items-center gap-1 rounded border border-default bg-elevated px-2 py-1 text-[10px] font-medium text-ink-secondary hover:bg-surface hover:text-ink-primary disabled:opacity-50"
                  >
                    <Eye size={11} />
                    View
                  </button>
                )}
                {/* "Download" */}
                <button
                  type="button"
                  onClick={() => handleDownload(form.key, form.isZip)}
                  disabled={isBusy || busy !== null}
                  title={form.isZip ? "Download zip" : "Download PDF"}
                  className={cn(
                    "inline-flex items-center gap-1 rounded border px-2 py-1 text-[10px] font-medium disabled:opacity-50",
                    form.isZip
                      ? "border-brand-500/50 bg-brand-500/15 text-brand-300 hover:bg-brand-500/25"
                      : "border-default bg-elevated text-ink-secondary hover:bg-surface hover:text-ink-primary"
                  )}
                >
                  <Download size={11} />
                  {isBusy ? "…" : form.isZip ? "Download" : "PDF"}
                </button>
              </div>
            </div>
          );
        })}
      </div>

      {/* Error display */}
      {error && (
        <div className="flex items-start gap-2 rounded-md border border-debit/40 bg-debit/[0.07] px-3 py-2">
          <AlertTriangle size={13} className="mt-0.5 shrink-0 text-debit" />
          <div className="min-w-0">
            <p className="text-xs font-medium text-debit">Download failed</p>
            <p className="mt-0.5 text-[11px] text-debit/80">{error}</p>
            <p className="mt-1 text-[10px] text-ink-tertiary">
              Make sure the IRS form templates are present on the server
              (run <code className="font-mono">scripts/download_irs_forms.sh</code>).
            </p>
          </div>
        </div>
      )}

      {/* Field-mapping note */}
      <p className="text-[10px] text-ink-tertiary leading-relaxed">
        Fields pre-filled from the tax review queue (agent-proposed amounts) and
        the CPA export package. Lines without data are left blank for the CPA.
        EIN and officer SSNs must be entered manually — encrypted fields are never
        transmitted to PDFs. The CPA completes Lines 29–38 (tax computation,
        NOL, and payments) before filing. Use "View" to open a PDF in-browser;
        use "PDF" / "Download" to save locally.
      </p>
    </div>
  );
}
