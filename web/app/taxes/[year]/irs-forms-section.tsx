"use client";

/**
 * IrsFormsSection — "IRS Forms" card in the /taxes/[year] page.
 *
 * Shows download buttons for:
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
 * Every form carries a DRAFT watermark — the UI surfaces this reminder.
 */

import { useState } from "react";
import { Download, FileText, Package, AlertTriangle } from "lucide-react";
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

  async function handleDownload(formKey: string, isZip?: boolean) {
    setError(null);
    setBusy(formKey);
    try {
      const url =
        `/api/workspaces/${encodeURIComponent(workspaceId)}` +
        `/entities/${encodeURIComponent(entityId)}` +
        `/irs-forms/${encodeURIComponent(year)}` +
        `/${encodeURIComponent(formKey)}.pdf`;

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

      {/* Form download buttons */}
      <div className="grid gap-2 sm:grid-cols-2">
        {FORMS.map((form) => {
          const isBusy = busy === form.key;
          return (
            <button
              key={form.key}
              type="button"
              onClick={() => handleDownload(form.key, form.isZip)}
              disabled={isBusy || busy !== null}
              className={cn(
                "group flex w-full items-center gap-3 rounded-lg border px-3 py-3 text-left",
                "transition-colors",
                form.isZip
                  ? "border-brand-500/50 bg-brand-500/[0.06] hover:bg-brand-500/[0.12] disabled:opacity-50"
                  : "border-default bg-surface hover:bg-elevated disabled:opacity-50"
              )}
            >
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
              <Download
                size={13}
                className={cn(
                  "shrink-0 transition-opacity",
                  isBusy
                    ? "opacity-0"
                    : "opacity-0 group-hover:opacity-100 text-ink-tertiary"
                )}
              />
            </button>
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
        NOL, and payments) before filing.
      </p>
    </div>
  );
}
