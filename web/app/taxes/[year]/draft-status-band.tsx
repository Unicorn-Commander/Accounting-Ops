"use client";

// DraftStatusBand — Tier-2 audit H8.
//
// Persistent, unmistakable, full-width status band rendered ABOVE the
// narrative body (and any print render). Replaces the small "DRAFT stamp
// present" pill that the audit flagged as compliance metadata rather than
// user-facing assurance.
//
// Visible in print mode: this is the most important property — when a CPA
// prints the narrative for archive/review, the DRAFT — NOT FILED — CPA
// SIGN-OFF REQUIRED legend MUST survive even if the dark theme is stripped.

import { AlertTriangle, Download } from "lucide-react";
import { cn } from "@/lib/cn";

export type DraftStatusBandProps = {
  /** Tax year, e.g. 2023. */
  year: number;
  /** ISO timestamp when the underlying narrative was last generated, if known. */
  generatedAt?: string | null;
  /** Model id used to generate the narrative (e.g. "claude-3.7-sonnet"). */
  model?: string | null;
  /** Count of unresolved items in tax_review_queue for this year. */
  unresolvedCount: number;
  /**
   * Click handler for the Export Reviewed Package button (typically opens the
   * handoff package download). Optional — when absent the button hides itself
   * (e.g. on a consolidated view where no entity-scoped package exists).
   */
  onExportReviewedPackage?: () => void;
  /** Whether the export action is currently busy. */
  exportBusy?: boolean;
  /** Optional CSS classes appended to the outer container. */
  className?: string;
};

function fmtDateTime(iso?: string | null): string | null {
  if (!iso) return null;
  try {
    const d = new Date(iso);
    return d.toLocaleString(undefined, {
      year: "numeric",
      month: "short",
      day: "numeric",
      hour: "2-digit",
      minute: "2-digit",
    });
  } catch {
    return iso;
  }
}

export function DraftStatusBand({
  year,
  generatedAt,
  model,
  unresolvedCount,
  onExportReviewedPackage,
  exportBusy,
  className,
}: DraftStatusBandProps) {
  const generated = fmtDateTime(generatedAt);
  return (
    <div
      // The band is intentionally LOUD: thick warning border, large bold
      // legend, persistent role="status". Survives `print:` styles via
      // explicit border + background overrides in globals.css.
      className={cn(
        "rounded-lg border-2 border-pending bg-pending/[0.08] px-4 py-3 md:px-5 md:py-3.5",
        "print:border-2 print:border-amber-600 print:bg-amber-50 print:text-amber-900",
        className
      )}
      role="status"
      aria-live="polite"
      data-draft-band={year}
    >
      <div className="flex flex-col gap-2 md:flex-row md:items-center md:justify-between">
        {/* Left: alarm + headline copy */}
        <div className="flex items-start gap-2.5 md:items-center">
          <AlertTriangle
            size={20}
            className="mt-0.5 shrink-0 text-pending print:text-amber-700 md:mt-0"
            aria-hidden="true"
          />
          <div className="min-w-0">
            <p className="text-sm font-bold uppercase tracking-wide text-pending print:text-amber-900">
              Draft — not filed — CPA sign-off required
            </p>
            <p className="mt-0.5 text-[11px] text-ink-secondary print:text-amber-800">
              {year} narrative draft. Nothing here has been submitted to the
              IRS or any state agency.
            </p>
          </div>
        </div>

        {/* Right: export action */}
        {onExportReviewedPackage ? (
          <button
            type="button"
            onClick={onExportReviewedPackage}
            disabled={exportBusy}
            className={cn(
              "inline-flex shrink-0 items-center gap-1.5 self-start rounded-md border border-pending/60 bg-surface px-3 py-1.5 text-xs font-medium text-pending transition hover:bg-pending/10 disabled:opacity-50",
              "print:hidden md:self-auto"
            )}
          >
            <Download size={12} />
            {exportBusy ? "Building…" : "Export reviewed package"}
          </button>
        ) : null}
      </div>

      {/* Metadata strip: provenance + open items */}
      <div className="mt-2 flex flex-wrap items-center gap-x-3 gap-y-1 border-t border-pending/30 pt-2 text-[11px] text-ink-secondary print:border-amber-300 print:text-amber-900">
        {generated ? (
          <span>
            <span className="text-ink-tertiary print:text-amber-700">
              Generated
            </span>{" "}
            {generated}
          </span>
        ) : null}
        {model ? (
          <>
            <span className="text-ink-tertiary print:text-amber-700">·</span>
            <span>
              <span className="text-ink-tertiary print:text-amber-700">
                Model
              </span>{" "}
              <span className="font-mono">{model}</span>
            </span>
          </>
        ) : null}
        <span className="text-ink-tertiary print:text-amber-700">·</span>
        <span
          className={cn(
            unresolvedCount > 0
              ? "font-medium text-debit print:text-red-800"
              : "text-credit print:text-green-900"
          )}
        >
          {unresolvedCount === 0
            ? "All review items resolved"
            : `${unresolvedCount} unresolved review item${unresolvedCount === 1 ? "" : "s"}`}
        </span>
      </div>
    </div>
  );
}
