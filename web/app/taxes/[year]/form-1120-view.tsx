"use client";

// Form1120View — interactive CPA review package for Form 1120.
//
// Features:
//   - Full Income / Deductions / Tax & Payments / Schedule L rendering
//   - Expandable "Source" panels per line showing COA categories + entry counts
//   - Yellow info badges for the 4 CPA-review categories
//   - Per-line reviewer overrides saved to localStorage
//   - Download JSON (raw package + overrides)
//   - Download PDF (jspdf-autotable tabular form)
//   - Print-friendly styles (no nav, no buttons, disclaimer + overrides included)

import { useCallback, useEffect, useMemo, useState } from "react";
import {
  AlertTriangle,
  ChevronDown,
  ChevronRight,
  Download,
  FileJson,
  Info,
  Pencil,
  Printer,
  X
} from "lucide-react";
import { cn } from "@/lib/cn";
import { Card } from "@/components/primitives";
import {
  type Form1120Package,
  type Form1120Line,
  type Form1120LineDetail,
  CPA_REVIEW_CATEGORIES
} from "@/lib/taxes-1120";

// ---------------------------------------------------------------------------
// Types
// ---------------------------------------------------------------------------

export type Form1120ViewProps = {
  pkg: Form1120Package | null;
  workspaceId: string;
  workspaceSlug: string;
  entityId: string;
  entityDisplayName: string;
  year: number;
};

type Override = {
  amount_cents: number | null; // null = keep original
  note: string;
  reviewer: string;
  overridden_at: string;
};

type OverrideMap = Record<string, Override>;

// ---------------------------------------------------------------------------
// LocalStorage key helpers
// ---------------------------------------------------------------------------

function overrideKey(
  workspaceId: string,
  entityId: string,
  year: number,
  line: string
): string {
  return `aops:1120-override:${workspaceId}:${entityId}:${year}:${line}`;
}

function loadOverrides(
  workspaceId: string,
  entityId: string,
  year: number
): OverrideMap {
  if (typeof window === "undefined") return {};
  const result: OverrideMap = {};
  const prefix = `aops:1120-override:${workspaceId}:${entityId}:${year}:`;
  for (let i = 0; i < window.localStorage.length; i++) {
    const k = window.localStorage.key(i);
    if (k && k.startsWith(prefix)) {
      const line = k.slice(prefix.length);
      try {
        const raw = window.localStorage.getItem(k);
        if (raw) result[line] = JSON.parse(raw) as Override;
      } catch {
        // skip corrupt entries
      }
    }
  }
  return result;
}

function saveOverride(
  workspaceId: string,
  entityId: string,
  year: number,
  line: string,
  override: Override
): void {
  if (typeof window === "undefined") return;
  try {
    window.localStorage.setItem(
      overrideKey(workspaceId, entityId, year, line),
      JSON.stringify(override)
    );
  } catch {
    // localStorage unavailable in some envs — best-effort
  }
}

function clearOverride(
  workspaceId: string,
  entityId: string,
  year: number,
  line: string
): void {
  if (typeof window === "undefined") return;
  try {
    window.localStorage.removeItem(overrideKey(workspaceId, entityId, year, line));
  } catch {
    // ignore
  }
}

// ---------------------------------------------------------------------------
// Number formatters
// ---------------------------------------------------------------------------

function formatCents(cents: number): string {
  const dollars = cents / 100;
  const abs = Math.abs(dollars);
  const formatted = new Intl.NumberFormat("en-US", {
    style: "currency",
    currency: "USD",
    minimumFractionDigits: 0,
    maximumFractionDigits: 0
  }).format(abs);
  return dollars < 0 ? `(${formatted})` : formatted;
}

function parseDollarsInput(raw: string): number | null {
  const cleaned = raw.replace(/[$,\s()]/g, "").replace(/^\((.+)\)$/, "-$1");
  const n = parseFloat(cleaned);
  if (isNaN(n)) return null;
  return Math.round(n * 100);
}

// ---------------------------------------------------------------------------
// CPA review badge categories to flag — check if any detail category is flagged
// ---------------------------------------------------------------------------

function getCpaBadgeForLine(
  detail: Form1120LineDetail[] | undefined
): string | null {
  if (!detail) return null;
  for (const d of detail) {
    const msg = CPA_REVIEW_CATEGORIES[d.category];
    if (msg) return msg;
  }
  return null;
}

// Check if a line's detail contains any CPA-flagged categories
function getCpaFlaggedCategories(
  detail: Form1120LineDetail[] | undefined
): Array<{ category: string; note: string }> {
  if (!detail) return [];
  return detail
    .filter((d) => CPA_REVIEW_CATEGORIES[d.category])
    .map((d) => ({ category: d.category, note: CPA_REVIEW_CATEGORIES[d.category] }));
}

// ---------------------------------------------------------------------------
// Override dialog
// ---------------------------------------------------------------------------

function OverrideDialog({
  line,
  label,
  originalCents,
  existing,
  onSave,
  onClear,
  onClose
}: {
  line: string;
  label: string;
  originalCents: number;
  existing: Override | null;
  onSave: (o: Override) => void;
  onClear: () => void;
  onClose: () => void;
}) {
  const [amountStr, setAmountStr] = useState<string>(
    existing?.amount_cents != null
      ? (existing.amount_cents / 100).toFixed(2)
      : (originalCents / 100).toFixed(2)
  );
  const [note, setNote] = useState(existing?.note ?? "");
  const [reviewer, setReviewer] = useState(existing?.reviewer ?? "");
  const [err, setErr] = useState<string | null>(null);

  function handleSave() {
    const cents = parseDollarsInput(amountStr);
    if (cents === null) {
      setErr("Enter a valid dollar amount (e.g. 12500 or 12500.00).");
      return;
    }
    onSave({
      amount_cents: cents,
      note,
      reviewer,
      overridden_at: new Date().toISOString()
    });
  }

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center p-4">
      {/* Backdrop */}
      <button
        type="button"
        className="absolute inset-0 bg-canvas/80 backdrop-blur-sm"
        onClick={onClose}
        aria-label="Close"
      />
      <div className="relative z-10 w-full max-w-md rounded-xl border border-default bg-surface p-6 shadow-card">
        <div className="mb-4 flex items-start justify-between gap-3">
          <div>
            <p className="text-[10px] font-medium uppercase tracking-wider text-ink-tertiary">
              Reviewer override · Line {line}
            </p>
            <h2 className="mt-0.5 text-base font-semibold leading-snug">{label}</h2>
          </div>
          <button
            type="button"
            onClick={onClose}
            className="text-ink-tertiary hover:text-ink-primary"
            aria-label="Close"
          >
            <X size={16} />
          </button>
        </div>

        <div className="rounded-md border border-subtle bg-elevated px-3 py-2 text-xs text-ink-secondary mb-4">
          <span className="text-ink-tertiary">Original: </span>
          <span className="font-mono">{formatCents(originalCents)}</span>
        </div>

        <div className="space-y-3">
          <label className="block text-xs text-ink-secondary">
            Override amount ($)
            <input
              type="text"
              value={amountStr}
              onChange={(e) => {
                setAmountStr(e.target.value);
                setErr(null);
              }}
              className="mt-1 block w-full rounded-md border border-default bg-elevated px-3 py-1.5 font-mono text-sm text-ink-primary placeholder-ink-disabled focus:border-brand-500 focus:outline-none"
              placeholder="12500.00"
            />
          </label>
          <label className="block text-xs text-ink-secondary">
            Note (reason for override)
            <textarea
              value={note}
              onChange={(e) => setNote(e.target.value)}
              rows={2}
              className="mt-1 block w-full rounded-md border border-default bg-elevated px-3 py-1.5 text-sm text-ink-primary placeholder-ink-disabled focus:border-brand-500 focus:outline-none resize-none"
              placeholder="e.g. Removed 50% meals haircut per §274(n)"
            />
          </label>
          <label className="block text-xs text-ink-secondary">
            Reviewer name / initials
            <input
              type="text"
              value={reviewer}
              onChange={(e) => setReviewer(e.target.value)}
              className="mt-1 block w-full rounded-md border border-default bg-elevated px-3 py-1.5 text-sm text-ink-primary placeholder-ink-disabled focus:border-brand-500 focus:outline-none"
              placeholder="CPA initials or name"
            />
          </label>
        </div>

        {err && (
          <p className="mt-2 text-xs text-debit">{err}</p>
        )}

        <p className="mt-3 text-[10px] text-ink-tertiary">
          Overrides are saved in your browser only (localStorage). They do not
          modify the journal ledger. Ledger corrections go through /approvals.
        </p>

        <div className="mt-5 flex items-center justify-between gap-3">
          {existing && (
            <button
              type="button"
              onClick={onClear}
              className="text-xs text-debit hover:underline"
            >
              Clear override
            </button>
          )}
          <div className="ml-auto flex items-center gap-2">
            <button
              type="button"
              onClick={onClose}
              className="rounded-md border border-default bg-elevated px-3 py-1.5 text-xs text-ink-secondary hover:text-ink-primary"
            >
              Cancel
            </button>
            <button
              type="button"
              onClick={handleSave}
              className="rounded-md bg-brand-500 px-3 py-1.5 text-xs font-medium text-white hover:bg-brand-600"
            >
              Save override
            </button>
          </div>
        </div>
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Single form line row
// ---------------------------------------------------------------------------

function FormLineRow({
  lineData,
  detail,
  overrides,
  workspaceId,
  entityId,
  year,
  onOverrideChange
}: {
  lineData: Form1120Line;
  detail: Form1120LineDetail[] | undefined;
  overrides: OverrideMap;
  workspaceId: string;
  entityId: string;
  year: number;
  onOverrideChange: (line: string, o: Override | null) => void;
}) {
  const [expanded, setExpanded] = useState(false);
  const [showOverrideDialog, setShowOverrideDialog] = useState(false);

  const override = overrides[lineData.line] ?? null;
  const displayCents =
    override?.amount_cents != null ? override.amount_cents : lineData.amount_cents;

  const cpaBadges = getCpaFlaggedCategories(detail);

  return (
    <>
      <tr
        className={cn(
          "group border-b border-subtle/40 last:border-b-0",
          expanded && "bg-elevated/40"
        )}
      >
        {/* Line number */}
        <td className="w-12 px-4 py-2.5 align-top">
          <span className="font-mono text-[11px] text-ink-tertiary">
            {lineData.line}
          </span>
        </td>

        {/* Label + CPA badges */}
        <td className="px-4 py-2.5 align-top">
          <div className="flex flex-wrap items-start gap-2">
            <span className="text-sm text-ink-primary">{lineData.label}</span>
            {cpaBadges.map((b) => (
              <span
                key={b.category}
                title={b.note}
                className="inline-flex cursor-help items-center gap-1 rounded-full border border-pending/40 bg-pending/[0.08] px-2 py-0.5 text-[10px] font-medium text-pending"
              >
                <Info size={10} />
                {b.category}
              </span>
            ))}
            {override && (
              <span className="inline-flex items-center gap-1 rounded-full border border-brand-500/40 bg-brand-500/10 px-2 py-0.5 text-[10px] font-medium text-brand-300">
                Overridden by reviewer
                {override.reviewer ? ` (${override.reviewer})` : ""}
              </span>
            )}
          </div>

          {/* Source expand toggle */}
          {detail && detail.length > 0 && (
            <button
              type="button"
              onClick={() => setExpanded((v) => !v)}
              className="mt-1 inline-flex items-center gap-1 text-[11px] text-ink-tertiary hover:text-ink-primary"
            >
              {expanded ? <ChevronDown size={11} /> : <ChevronRight size={11} />}
              Source ({detail.length} categor{detail.length !== 1 ? "ies" : "y"})
            </button>
          )}
        </td>

        {/* Amount */}
        <td className="w-36 px-4 py-2.5 text-right align-top">
          <span
            className={cn(
              "font-mono tabular-nums text-sm",
              override?.amount_cents != null && "text-brand-300"
            )}
          >
            {formatCents(displayCents)}
          </span>
          {override?.amount_cents != null && (
            <div className="mt-0.5 font-mono text-[10px] text-ink-tertiary line-through">
              {formatCents(lineData.amount_cents)}
            </div>
          )}
        </td>

        {/* Override action */}
        <td className="w-10 px-2 py-2.5 align-top print:hidden">
          <button
            type="button"
            title="Add reviewer override"
            onClick={() => setShowOverrideDialog(true)}
            className="invisible text-ink-tertiary hover:text-brand-300 group-hover:visible"
          >
            <Pencil size={13} />
          </button>
        </td>
      </tr>

      {/* Source panel */}
      {expanded && detail && detail.length > 0 && (
        <tr className="border-b border-subtle/40">
          <td />
          <td colSpan={3} className="bg-elevated/30 px-4 pb-3 pt-1">
            <table className="w-full text-[11px]">
              <thead>
                <tr className="text-ink-tertiary">
                  <th className="pb-1 text-left font-medium">Category</th>
                  <th className="pb-1 text-right font-medium">Amount</th>
                  <th className="pb-1 text-right font-medium">Entries</th>
                </tr>
              </thead>
              <tbody>
                {detail.map((d) => {
                  const isFlagged = !!CPA_REVIEW_CATEGORIES[d.category];
                  return (
                    <tr key={d.category} className={cn(isFlagged && "text-pending")}>
                      <td className="py-0.5 font-mono">{d.category}</td>
                      <td className="py-0.5 text-right font-mono tabular-nums">
                        {formatCents(d.amount_cents)}
                      </td>
                      <td className="py-0.5 text-right font-mono tabular-nums text-ink-tertiary">
                        {d.entry_count}
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
            {override?.note && (
              <p className="mt-2 text-[10px] text-brand-300">
                Override note: {override.note}
              </p>
            )}
          </td>
        </tr>
      )}

      {/* Override dialog */}
      {showOverrideDialog && (
        <tr>
          <td colSpan={4}>
            <OverrideDialog
              line={lineData.line}
              label={lineData.label}
              originalCents={lineData.amount_cents}
              existing={override}
              onSave={(o) => {
                saveOverride(workspaceId, entityId, year, lineData.line, o);
                onOverrideChange(lineData.line, o);
                setShowOverrideDialog(false);
              }}
              onClear={() => {
                clearOverride(workspaceId, entityId, year, lineData.line);
                onOverrideChange(lineData.line, null);
                setShowOverrideDialog(false);
              }}
              onClose={() => setShowOverrideDialog(false)}
            />
          </td>
        </tr>
      )}
    </>
  );
}

// ---------------------------------------------------------------------------
// Section header row
// ---------------------------------------------------------------------------

function SectionHeadRow({
  label,
  color = "ink"
}: {
  label: string;
  color?: "income" | "deductions" | "tax" | "ink";
}) {
  const cls =
    color === "income"
      ? "text-credit"
      : color === "deductions"
        ? "text-debit"
        : color === "tax"
          ? "text-brand-300"
          : "text-ink-secondary";
  return (
    <tr className="bg-elevated/60">
      <td
        colSpan={4}
        className={cn(
          "px-4 py-2 text-[10px] font-semibold uppercase tracking-wider",
          cls
        )}
      >
        {label}
      </td>
    </tr>
  );
}

function TotalRow({
  label,
  cents,
  tone = "ink"
}: {
  label: string;
  cents: number;
  tone?: "credit" | "debit" | "brand" | "ink";
}) {
  const cls =
    tone === "credit"
      ? "text-credit"
      : tone === "debit"
        ? "text-debit"
        : tone === "brand"
          ? "text-brand-300"
          : "text-ink-primary";
  return (
    <tr
      className={cn(
        "border-t-2",
        tone === "credit"
          ? "border-credit/30 bg-credit/[0.04]"
          : tone === "debit"
            ? "border-debit/30 bg-debit/[0.04]"
            : tone === "brand"
              ? "border-brand-500/30 bg-brand-500/[0.04]"
              : "border-subtle bg-elevated"
      )}
    >
      <td colSpan={2} className="px-4 py-2 text-xs font-semibold uppercase tracking-wider">
        {label}
      </td>
      <td className={cn("px-4 py-2 text-right font-mono tabular-nums font-bold text-base", cls)}>
        {formatCents(cents)}
      </td>
      <td className="print:hidden" />
    </tr>
  );
}

function PlaceholderRow({ line, label }: { line: string; label: string }) {
  return (
    <tr className="border-b border-subtle/40 opacity-40">
      <td className="w-12 px-4 py-2 align-top">
        <span className="font-mono text-[11px] text-ink-tertiary">{line}</span>
      </td>
      <td className="px-4 py-2 align-top">
        <span className="text-sm text-ink-secondary">{label}</span>
      </td>
      <td className="w-36 px-4 py-2 text-right align-top">
        <span className="font-mono text-xs text-ink-disabled">CPA fills</span>
      </td>
      <td className="w-10 px-2 py-2 print:hidden" />
    </tr>
  );
}

// ---------------------------------------------------------------------------
// PDF export helper (jspdf-autotable)
// ---------------------------------------------------------------------------

async function downloadPdf(
  pkg: Form1120Package,
  overrides: OverrideMap,
  workspaceSlug: string,
  entityDisplayName: string,
  year: number
): Promise<void> {
  // Lazy-load jspdf + jspdf-autotable only when the user actually clicks
  const { default: jsPDF } = await import("jspdf");
  const { default: autoTable } = await import("jspdf-autotable");

  const doc = new jsPDF({ orientation: "portrait", unit: "mm", format: "letter" });
  const pageWidth = doc.internal.pageSize.getWidth();

  // Helper to get effective cents
  function effectiveCents(line: string, original: number): number {
    const ov = overrides[line];
    return ov?.amount_cents != null ? ov.amount_cents : original;
  }

  function dollarsStr(cents: number): string {
    const dollars = cents / 100;
    const abs = Math.abs(dollars);
    const formatted = new Intl.NumberFormat("en-US", {
      style: "currency",
      currency: "USD",
      minimumFractionDigits: 0,
      maximumFractionDigits: 0
    }).format(abs);
    return dollars < 0 ? `(${formatted})` : formatted;
  }

  const generatedAt = new Date().toLocaleString("en-US", { timeZoneName: "short" });

  // Title block
  doc.setFontSize(14);
  doc.setFont("helvetica", "bold");
  doc.text("Form 1120 — U.S. Corporation Income Tax Return", 14, 18);
  doc.setFontSize(9);
  doc.setFont("helvetica", "normal");
  doc.text(
    `Tax Year: ${year}  |  Entity: ${entityDisplayName}  |  Workspace: ${workspaceSlug}`,
    14,
    25
  );
  doc.text(`Generated: ${generatedAt}`, 14, 30);

  // Disclaimer banner
  doc.setFillColor(255, 200, 60);
  doc.setTextColor(80, 50, 0);
  doc.setFontSize(8);
  doc.setFont("helvetica", "bold");
  doc.text("DRAFT — FOR CPA REVIEW ONLY. NOT A FILED RETURN.", pageWidth / 2, 37, {
    align: "center"
  });
  doc.setTextColor(0, 0, 0);
  doc.setFont("helvetica", "normal");

  let startY = 42;

  // Build rows helper
  function buildRows(lines: Form1120Line[]): string[][] {
    return lines.map((l) => {
      const eff = effectiveCents(l.line, l.amount_cents);
      const ov = overrides[l.line];
      const amtStr =
        ov?.amount_cents != null
          ? `${dollarsStr(eff)} [overridden from ${dollarsStr(l.amount_cents)}]`
          : dollarsStr(eff);
      return [l.line, l.label, amtStr];
    });
  }

  // Income section
  doc.setFontSize(9);
  doc.setFont("helvetica", "bold");
  doc.text("INCOME", 14, startY);
  startY += 2;

  autoTable(doc, {
    startY,
    head: [["Line", "Description", "Amount"]],
    body: buildRows(pkg.income.lines),
    foot: [["", "TOTAL INCOME", dollarsStr(pkg.income.total_income_cents)]],
    headStyles: { fillColor: [40, 80, 50], textColor: 255, fontSize: 7 },
    footStyles: { fillColor: [40, 80, 50], textColor: 255, fontSize: 7, fontStyle: "bold" },
    styles: { fontSize: 7, cellPadding: 1.5 },
    columnStyles: { 0: { cellWidth: 12 }, 2: { halign: "right", cellWidth: 35 } },
    didDrawPage: () => { /* intentionally empty */ }
  });

  startY = (doc as unknown as { lastAutoTable: { finalY: number } }).lastAutoTable.finalY + 6;

  // Deductions section
  doc.setFont("helvetica", "bold");
  doc.text("DEDUCTIONS", 14, startY);
  startY += 2;

  autoTable(doc, {
    startY,
    head: [["Line", "Description", "Amount"]],
    body: buildRows(pkg.deductions.lines),
    foot: [["", "TOTAL DEDUCTIONS", dollarsStr(pkg.deductions.total_deductions_cents)]],
    headStyles: { fillColor: [80, 30, 30], textColor: 255, fontSize: 7 },
    footStyles: { fillColor: [80, 30, 30], textColor: 255, fontSize: 7, fontStyle: "bold" },
    styles: { fontSize: 7, cellPadding: 1.5 },
    columnStyles: { 0: { cellWidth: 12 }, 2: { halign: "right", cellWidth: 35 } }
  });

  startY = (doc as unknown as { lastAutoTable: { finalY: number } }).lastAutoTable.finalY + 6;

  // Taxable income
  doc.setFont("helvetica", "bold");
  doc.text(
    `Line 28 — Taxable Income Before NOL: ${dollarsStr(pkg.taxable_income_before_nol_cents)}`,
    14,
    startY
  );
  startY += 8;

  // Schedule L
  if (startY > 220) {
    doc.addPage();
    startY = 18;
  }

  doc.setFont("helvetica", "bold");
  doc.text("SCHEDULE L — BALANCE SHEET (approximate from journal entries)", 14, startY);
  startY += 2;

  autoTable(doc, {
    startY,
    head: [["Line", "Description", "Amount"]],
    body: [
      ...pkg.schedule_l.assets.map((l) => [l.line, l.label, dollarsStr(l.amount_cents)]),
      ["", "TOTAL ASSETS", dollarsStr(pkg.schedule_l.total_assets_cents)],
      ...pkg.schedule_l.liabilities_equity.map((l) => [
        l.line,
        l.label,
        dollarsStr(l.amount_cents)
      ]),
      ["", "TOTAL LIABILITIES & EQUITY", dollarsStr(pkg.schedule_l.total_liabilities_equity_cents)]
    ],
    headStyles: { fillColor: [40, 40, 80], textColor: 255, fontSize: 7 },
    styles: { fontSize: 7, cellPadding: 1.5 },
    columnStyles: { 0: { cellWidth: 12 }, 2: { halign: "right", cellWidth: 35 } }
  });

  startY = (doc as unknown as { lastAutoTable: { finalY: number } }).lastAutoTable.finalY + 6;

  // Reviewer overrides summary
  const overrideEntries = Object.entries(overrides);
  if (overrideEntries.length > 0) {
    if (startY > 220) {
      doc.addPage();
      startY = 18;
    }
    doc.setFont("helvetica", "bold");
    doc.text("REVIEWER OVERRIDES", 14, startY);
    startY += 2;

    autoTable(doc, {
      startY,
      head: [["Line", "Original", "Override", "Reviewer", "Note", "Date"]],
      body: overrideEntries.map(([line, ov]) => {
        const originalLine = [
          ...pkg.income.lines,
          ...pkg.deductions.lines
        ].find((l) => l.line === line);
        const originalCents = originalLine?.amount_cents ?? 0;
        return [
          line,
          dollarsStr(originalCents),
          ov.amount_cents != null ? dollarsStr(ov.amount_cents) : "—",
          ov.reviewer || "—",
          ov.note || "—",
          ov.overridden_at ? new Date(ov.overridden_at).toLocaleDateString() : "—"
        ];
      }),
      headStyles: { fillColor: [60, 40, 100], textColor: 255, fontSize: 7 },
      styles: { fontSize: 6, cellPadding: 1 }
    });

    startY = (doc as unknown as { lastAutoTable: { finalY: number } }).lastAutoTable.finalY + 6;
  }

  // Footer disclaimer (all pages)
  const totalPages = doc.getNumberOfPages();
  for (let p = 1; p <= totalPages; p++) {
    doc.setPage(p);
    doc.setFontSize(6);
    doc.setFont("helvetica", "italic");
    doc.setTextColor(120);
    const disclaimerLines = doc.splitTextToSize(pkg.disclaimer, pageWidth - 28);
    doc.text(disclaimerLines, 14, doc.internal.pageSize.getHeight() - 10);
    doc.text(
      `Page ${p} of ${totalPages}`,
      pageWidth - 14,
      doc.internal.pageSize.getHeight() - 6,
      { align: "right" }
    );
    doc.setTextColor(0);
  }

  const slug = workspaceSlug.toLowerCase().replace(/\s+/g, "-");
  doc.save(`accounting-ops-1120-${slug}-${year}.pdf`);
}

// ---------------------------------------------------------------------------
// Main component
// ---------------------------------------------------------------------------

export function Form1120View({
  pkg,
  workspaceId,
  workspaceSlug,
  entityId,
  entityDisplayName,
  year
}: Form1120ViewProps) {
  const [overrides, setOverrides] = useState<OverrideMap>({});
  const [pdfBusy, setPdfBusy] = useState(false);

  // Load overrides from localStorage on mount
  useEffect(() => {
    setOverrides(loadOverrides(workspaceId, entityId, year));
  }, [workspaceId, entityId, year]);

  const handleOverrideChange = useCallback(
    (line: string, o: Override | null) => {
      setOverrides((prev) => {
        const next = { ...prev };
        if (o === null) {
          delete next[line];
        } else {
          next[line] = o;
        }
        return next;
      });
    },
    []
  );

  // Effective total income (apply overrides)
  const effectiveTotalIncome = useMemo(() => {
    if (!pkg) return 0;
    return pkg.income.lines.reduce((sum, l) => {
      const ov = overrides[l.line];
      return sum + (ov?.amount_cents != null ? ov.amount_cents : l.amount_cents);
    }, 0);
  }, [pkg, overrides]);

  const effectiveTotalDeductions = useMemo(() => {
    if (!pkg) return 0;
    return pkg.deductions.lines.reduce((sum, l) => {
      const ov = overrides[l.line];
      return sum + (ov?.amount_cents != null ? ov.amount_cents : l.amount_cents);
    }, 0);
  }, [pkg, overrides]);

  const effectiveTaxableIncome = effectiveTotalIncome - effectiveTotalDeductions;
  const overrideCount = Object.keys(overrides).length;

  // Download JSON
  function downloadJson() {
    if (!pkg) return;
    const payload = {
      ...pkg,
      reviewer_overrides: overrides,
      exported_at: new Date().toISOString()
    };
    const blob = new Blob([JSON.stringify(payload, null, 2)], {
      type: "application/json"
    });
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = `accounting-ops-1120-${workspaceSlug}-${year}.json`;
    document.body.appendChild(a);
    a.click();
    document.body.removeChild(a);
    URL.revokeObjectURL(url);
  }

  async function handleDownloadPdf() {
    if (!pkg) return;
    setPdfBusy(true);
    try {
      await downloadPdf(pkg, overrides, workspaceSlug, entityDisplayName, year);
    } finally {
      setPdfBusy(false);
    }
  }

  if (!pkg) {
    return (
      <Card>
        <p className="text-sm text-ink-tertiary">
          No data available for {year}. Post journal entries for this entity and
          return here to review the Form 1120 package.
        </p>
      </Card>
    );
  }

  const noData =
    pkg.income.lines.length === 0 && pkg.deductions.lines.length === 0;

  return (
    <div className="space-y-6 print:space-y-4">
      {/* Draft disclaimer banner */}
      <div className="rounded-lg border border-pending/50 bg-pending/[0.07] px-4 py-3 text-sm font-semibold text-pending print:rounded-none print:border-black print:bg-yellow-50 print:text-black">
        DRAFT — For CPA review only. Not a filed return.
      </div>

      {/* Disclaimer fine print */}
      <div className="rounded-md border border-subtle bg-elevated/50 px-4 py-3 text-[11px] text-ink-tertiary print:text-black">
        {pkg.disclaimer}
      </div>

      {/* Override count + export buttons */}
      <div className="flex flex-wrap items-center justify-between gap-3 print:hidden">
        <div className="text-xs text-ink-secondary">
          {overrideCount > 0 ? (
            <span className="inline-flex items-center gap-1.5 rounded-full border border-brand-500/40 bg-brand-500/10 px-2.5 py-1 text-brand-300">
              {overrideCount} reviewer override{overrideCount !== 1 ? "s" : ""} applied
            </span>
          ) : (
            <span className="text-ink-tertiary">No reviewer overrides yet. Click the pencil icon on any line to add one.</span>
          )}
        </div>
        <div className="flex items-center gap-2">
          <button
            type="button"
            onClick={() => window.print()}
            className="inline-flex items-center gap-1.5 rounded-md border border-default bg-elevated px-3 py-1.5 text-xs text-ink-secondary hover:text-ink-primary"
          >
            <Printer size={13} /> Print
          </button>
          <button
            type="button"
            onClick={downloadJson}
            className="inline-flex items-center gap-1.5 rounded-md border border-default bg-elevated px-3 py-1.5 text-xs text-ink-secondary hover:text-ink-primary"
          >
            <FileJson size={13} /> Download JSON
          </button>
          <button
            type="button"
            onClick={handleDownloadPdf}
            disabled={pdfBusy}
            className="inline-flex items-center gap-1.5 rounded-md bg-brand-500 px-3 py-1.5 text-xs font-medium text-white shadow-brand-glow hover:bg-brand-600 disabled:cursor-not-allowed disabled:bg-elevated disabled:text-ink-disabled disabled:shadow-none"
          >
            {pdfBusy ? (
              <span className="inline-block h-3 w-3 animate-spin rounded-full border border-white border-t-transparent" />
            ) : (
              <Download size={13} />
            )}
            {pdfBusy ? "Building PDF…" : "Download PDF"}
          </button>
        </div>
      </div>

      {noData && (
        <Card>
          <div className="flex items-center gap-2 text-pending">
            <AlertTriangle size={16} />
            <span className="text-sm font-medium">No data found for {year}</span>
          </div>
          <p className="mt-1 text-xs text-ink-secondary">
            The cpa-export service returned an empty package. Make sure journal
            entries are posted and the entity is correctly selected.
          </p>
        </Card>
      )}

      {/* INCOME SECTION */}
      {pkg.income.lines.length > 0 && (
        <Card padded={false}>
          <table className="w-full text-sm">
            <tbody>
              <SectionHeadRow label="Income (Lines 1a–11)" color="income" />
              {pkg.income.lines.map((l) => (
                <FormLineRow
                  key={l.line}
                  lineData={l}
                  detail={pkg.line_detail[l.line]}
                  overrides={overrides}
                  workspaceId={workspaceId}
                  entityId={entityId}
                  year={year}
                  onOverrideChange={handleOverrideChange}
                />
              ))}
              <TotalRow
                label="Total income (line 11)"
                cents={effectiveTotalIncome}
                tone="credit"
              />
            </tbody>
          </table>
        </Card>
      )}

      {/* DEDUCTIONS SECTION */}
      {pkg.deductions.lines.length > 0 && (
        <Card padded={false}>
          <table className="w-full text-sm">
            <tbody>
              <SectionHeadRow label="Deductions (Lines 12–29)" color="deductions" />
              {pkg.deductions.lines.map((l) => (
                <FormLineRow
                  key={l.line}
                  lineData={l}
                  detail={pkg.line_detail[l.line]}
                  overrides={overrides}
                  workspaceId={workspaceId}
                  entityId={entityId}
                  year={year}
                  onOverrideChange={handleOverrideChange}
                />
              ))}
              <TotalRow
                label="Total deductions (line 27)"
                cents={effectiveTotalDeductions}
                tone="debit"
              />
            </tbody>
          </table>
        </Card>
      )}

      {/* TAX & PAYMENTS SECTION */}
      <Card padded={false}>
        <table className="w-full text-sm">
          <tbody>
            <SectionHeadRow label="Tax &amp; Payments (Lines 28–38)" color="tax" />
            <tr className="border-b border-subtle/40">
              <td className="w-12 px-4 py-2.5 align-top">
                <span className="font-mono text-[11px] text-ink-tertiary">28</span>
              </td>
              <td className="px-4 py-2.5 align-top">
                <span className="text-sm text-ink-primary">
                  Taxable income before NOL deduction and special deductions
                </span>
              </td>
              <td className="w-36 px-4 py-2.5 text-right align-top">
                <span
                  className={cn(
                    "font-mono tabular-nums text-sm font-semibold",
                    effectiveTaxableIncome >= 0 ? "text-credit" : "text-debit"
                  )}
                >
                  {formatCents(effectiveTaxableIncome)}
                </span>
              </td>
              <td className="w-10 px-2 py-2.5 print:hidden" />
            </tr>
            <PlaceholderRow
              line="29a"
              label="Net operating loss (NOL) deduction"
            />
            <PlaceholderRow
              line="29b"
              label="Special deductions (Schedule C)"
            />
            <PlaceholderRow
              line="30"
              label="Taxable income (line 28 minus lines 29c)"
            />
            <PlaceholderRow
              line="31"
              label="Total tax (Schedule J, Part I, line 11)"
            />
            <PlaceholderRow
              line="33"
              label="Total payments and credits (Schedule J, Part II)"
            />
            <PlaceholderRow
              line="34"
              label="Estimated tax penalty"
            />
            <PlaceholderRow
              line="35"
              label="Amount owed"
            />
          </tbody>
        </table>
        <div className="border-t border-subtle px-4 py-2.5">
          <p className="text-[11px] text-ink-tertiary">
            Lines 29–38 require CPA input: NOL carryforwards, Schedule J tax
            computation, estimated tax payments, and any penalties. These are
            not computed by this platform.
          </p>
        </div>
      </Card>

      {/* SCHEDULE L */}
      {(pkg.schedule_l.assets.length > 0 ||
        pkg.schedule_l.liabilities_equity.length > 0) && (
        <Card padded={false}>
          <table className="w-full text-sm">
            <tbody>
              <SectionHeadRow
                label="Schedule L — Balance Sheet per Books (approximate)"
                color="ink"
              />

              {/* Assets */}
              {pkg.schedule_l.assets.map((l) => (
                <tr key={l.line} className="border-b border-subtle/40">
                  <td className="w-12 px-4 py-2.5 align-top">
                    <span className="font-mono text-[11px] text-ink-tertiary">{l.line}</span>
                  </td>
                  <td className="px-4 py-2.5 align-top text-sm text-ink-primary">
                    {l.label}
                  </td>
                  <td className="w-36 px-4 py-2.5 text-right align-top">
                    <span className="font-mono tabular-nums text-sm">
                      {formatCents(l.amount_cents)}
                    </span>
                  </td>
                  <td className="w-10 px-2 py-2.5 print:hidden" />
                </tr>
              ))}
              <TotalRow
                label="Total assets (line 15)"
                cents={pkg.schedule_l.total_assets_cents}
                tone="credit"
              />

              {/* Liabilities + Equity */}
              {pkg.schedule_l.liabilities_equity.map((l) => (
                <tr key={l.line} className="border-b border-subtle/40">
                  <td className="w-12 px-4 py-2.5 align-top">
                    <span className="font-mono text-[11px] text-ink-tertiary">{l.line}</span>
                  </td>
                  <td className="px-4 py-2.5 align-top text-sm text-ink-primary">
                    {l.label}
                  </td>
                  <td className="w-36 px-4 py-2.5 text-right align-top">
                    <span className="font-mono tabular-nums text-sm">
                      {formatCents(l.amount_cents)}
                    </span>
                  </td>
                  <td className="w-10 px-2 py-2.5 print:hidden" />
                </tr>
              ))}
              <TotalRow
                label="Total liabilities &amp; stockholders' equity (line 28)"
                cents={pkg.schedule_l.total_liabilities_equity_cents}
                tone="brand"
              />
            </tbody>
          </table>
          {pkg.schedule_l.note && (
            <div className="border-t border-subtle px-4 py-2.5">
              <p className="text-[11px] text-ink-tertiary">{pkg.schedule_l.note}</p>
            </div>
          )}
        </Card>
      )}

      {/* CPA review categories reference */}
      <Card>
        <h3 className="text-xs font-semibold uppercase tracking-wider text-pending mb-3">
          CPA Review Required — 4 Category Flags
        </h3>
        <div className="space-y-3">
          {Object.entries(CPA_REVIEW_CATEGORIES).map(([cat, note]) => (
            <div key={cat} className="flex gap-3">
              <span className="mt-0.5 shrink-0 text-pending">
                <Info size={14} />
              </span>
              <div>
                <p className="text-xs font-semibold text-pending">{cat}</p>
                <p className="mt-0.5 text-[11px] text-ink-secondary">{note}</p>
              </div>
            </div>
          ))}
        </div>
      </Card>

      {/* Generated metadata */}
      <p className="text-[10px] text-ink-tertiary print:text-black">
        Package generated: {new Date(pkg.generated_at).toLocaleString("en-US", {
          timeZoneName: "short"
        })} · workspace {pkg.workspace_id.slice(0, 8)}… · entity{" "}
        {pkg.entity_id === "consolidated" ? "consolidated" : pkg.entity_id.slice(0, 8) + "…"}
      </p>
    </div>
  );
}
