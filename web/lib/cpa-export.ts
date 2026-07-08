// Shared helpers for the CPA Export feature.
//
// Used by:
//   - app/cpa-export/page.tsx (server component, normalizes query params)
//   - app/cpa-export/cpa-export-client.tsx (client UI)
//   - app/api/cpa-export/[...path]/route.ts (proxy, builds upstream URL)
//   - tests/cpa-export.smoke.ts (smoke test on the helpers)

export type ExportFormat = "csv" | "iif" | "qbo" | "pl" | "balance-sheet" | "cash-flow";

/** Maps an ExportFormat to (backend path segment, filename extension, MIME hint). */
export const FORMAT_META: Record<
  ExportFormat,
  { backendPath: string; extension: string; mime: string; label: string }
> = {
  csv: { backendPath: "exports/csv", extension: "csv", mime: "text/csv", label: "CSV" },
  iif: { backendPath: "exports/iif", extension: "iif", mime: "application/octet-stream", label: "IIF" },
  qbo: { backendPath: "exports/qbo", extension: "qbo", mime: "application/xml", label: "QBO XML" },
  pl: {
    backendPath: "reports/pl.pdf",
    extension: "pl.pdf",
    mime: "application/pdf",
    label: "Profit & Loss"
  },
  "balance-sheet": {
    backendPath: "reports/balance-sheet.pdf",
    extension: "balance-sheet.pdf",
    mime: "application/pdf",
    label: "Balance Sheet"
  },
  "cash-flow": {
    backendPath: "reports/cash-flow.pdf",
    extension: "cash-flow.pdf",
    mime: "application/pdf",
    label: "Cash Flow"
  }
};

/**
 * Convert any string to a URL/filename-safe slug.
 *   lowercase → non-alphanumeric → "-" → collapse "-" runs → trim "-"
 */
export function slugify(input: string): string {
  return input
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, "-")
    .replace(/-+/g, "-")
    .replace(/^-+|-+$/g, "");
}

const PERIOD_RX = {
  ytd: /^ytd$/i,
  mtd: /^mtd$/i,
  qtd: /^qtd$/i,
  year: /^(\d{4})$/,
  quarter: /^(\d{4})-q([1-4])$/i,
  month: /^(\d{4})-(0[1-9]|1[0-2])$/,
  range: /^(\d{4}-\d{2}-\d{2})\.\.(\d{4}-\d{2}-\d{2})$/
};

/**
 * Validate + normalize a period string into a filename-safe slug.
 *   "ytd" / "mtd" / "qtd"           → "ytd" / "mtd" / "qtd"
 *   "2026"                          → "2026"
 *   "2026-Q2"                       → "2026-q2"
 *   "2026-05"                       → "2026-05"
 *   "2026-01-01..2026-05-22"        → "2026-01-01-to-2026-05-22"
 * Falls back to "ytd" when the input is empty/invalid.
 */
export function normalizePeriod(period: string | null | undefined): string {
  if (!period) return "ytd";
  const p = period.trim();
  if (PERIOD_RX.ytd.test(p)) return "ytd";
  if (PERIOD_RX.mtd.test(p)) return "mtd";
  if (PERIOD_RX.qtd.test(p)) return "qtd";
  if (PERIOD_RX.year.test(p)) return p;
  const q = p.match(PERIOD_RX.quarter);
  if (q) return `${q[1]}-q${q[2]}`;
  if (PERIOD_RX.month.test(p)) return p;
  const r = p.match(PERIOD_RX.range);
  if (r) return `${r[1]}-to-${r[2]}`;
  return "ytd";
}

/** True if the period string is a syntactically valid period (any accepted form). */
export function isValidPeriod(period: string | null | undefined): boolean {
  if (!period) return false;
  const p = period.trim();
  return (
    PERIOD_RX.ytd.test(p) ||
    PERIOD_RX.mtd.test(p) ||
    PERIOD_RX.qtd.test(p) ||
    PERIOD_RX.year.test(p) ||
    PERIOD_RX.quarter.test(p) ||
    PERIOD_RX.month.test(p) ||
    PERIOD_RX.range.test(p)
  );
}

/**
 * Build the on-disk filename for a download.
 * Convention: `accounting-ops-{workspaceSlug}-{entitySlug|"consolidated"}-{period}.{ext}`
 */
export function buildExportFilename(params: {
  workspaceSlug: string;
  entitySlug: string | null | undefined;
  period: string;
  format: ExportFormat;
}): string {
  const meta = FORMAT_META[params.format];
  const ws = slugify(params.workspaceSlug) || "workspace";
  const ent = slugify(params.entitySlug ?? "consolidated") || "consolidated";
  const period = normalizePeriod(params.period);
  return `accounting-ops-${ws}-${ent}-${period}.${meta.extension}`;
}

/**
 * Build the URLSearchParams for a backend request.
 * Backend expects:
 *   workspace_id={uuid}&period={string}&entity_id={uuid|"all"}
 */
export function buildExportSearchParams(params: {
  workspaceId: string;
  period: string;
  entityId: string | null | undefined;
}): URLSearchParams {
  const sp = new URLSearchParams();
  sp.set("workspace_id", params.workspaceId);
  sp.set("period", params.period || "ytd");
  // The backend's "all entities" sentinel is the string "all". Empty / "consolidated" both map to "all".
  const ent = !params.entityId || params.entityId === "consolidated" ? "all" : params.entityId;
  sp.set("entity_id", ent);
  return sp;
}
