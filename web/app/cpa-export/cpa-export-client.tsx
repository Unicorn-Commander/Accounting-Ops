"use client";

import * as Dialog from "@radix-ui/react-dialog";
import { useRouter, useSearchParams } from "next/navigation";
import { useCallback, useEffect, useMemo, useState, useTransition } from "react";
import {
  Code,
  Download,
  FileSpreadsheet,
  FileText,
  FileType,
  Loader2,
  X
} from "lucide-react";
import { Card } from "@/components/primitives";
import { cn } from "@/lib/cn";
import {
  buildExportFilename,
  buildExportSearchParams,
  FORMAT_META,
  isValidPeriod,
  type ExportFormat
} from "@/lib/cpa-export";

type QuickPick = {
  key: string;
  label: string;
  /** Resolved as of the time the chip is clicked. */
  resolve: () => string;
};

function thisQuarter(now: Date): string {
  const q = Math.floor(now.getMonth() / 3) + 1;
  return `${now.getFullYear()}-Q${q}`;
}

function lastQuarter(now: Date): string {
  const thisQ = Math.floor(now.getMonth() / 3) + 1;
  let year = now.getFullYear();
  let q = thisQ - 1;
  if (q === 0) {
    q = 4;
    year -= 1;
  }
  return `${year}-Q${q}`;
}

const QUICK_PICKS: QuickPick[] = [
  { key: "mtd", label: "MTD", resolve: () => "mtd" },
  { key: "qtd", label: "QTD", resolve: () => "qtd" },
  { key: "ytd", label: "YTD", resolve: () => "ytd" },
  { key: "this-q", label: "This Q", resolve: () => thisQuarter(new Date()) },
  { key: "last-q", label: "Last Q", resolve: () => lastQuarter(new Date()) },
  {
    key: "this-year",
    label: "This Year",
    resolve: () => String(new Date().getFullYear())
  },
  {
    key: "last-year",
    label: "Last Year",
    resolve: () => String(new Date().getFullYear() - 1)
  }
];

type RecentExport = { timestamp: number; format: string; filename: string };
const RECENT_KEY = "aops:cpa-export:recent";
const RECENT_MAX = 5;

function loadRecent(): RecentExport[] {
  if (typeof window === "undefined") return [];
  try {
    const raw = window.localStorage.getItem(RECENT_KEY);
    if (!raw) return [];
    const parsed = JSON.parse(raw) as RecentExport[];
    return Array.isArray(parsed) ? parsed.slice(0, RECENT_MAX) : [];
  } catch {
    return [];
  }
}

function saveRecent(list: RecentExport[]): void {
  if (typeof window === "undefined") return;
  try {
    window.localStorage.setItem(RECENT_KEY, JSON.stringify(list.slice(0, RECENT_MAX)));
  } catch {
    // localStorage may be unavailable (private mode, quota) — best-effort.
  }
}

export type CpaExportClientProps = {
  workspaceId: string;
  workspaceSlug: string;
  entityId: string | null; // null = consolidated
  entitySlug: string | null;
  entityDisplayName: string;
};

export function CpaExportClient({
  workspaceId,
  workspaceSlug,
  entityId,
  entitySlug,
  entityDisplayName
}: CpaExportClientProps) {
  const router = useRouter();
  const search = useSearchParams();
  const [, startTransition] = useTransition();

  const rawPeriod = search.get("period") ?? "ytd";
  const period = isValidPeriod(rawPeriod) ? rawPeriod : "ytd";

  // Custom range UI state.
  const isCustomRange = /^\d{4}-\d{2}-\d{2}\.\.\d{4}-\d{2}-\d{2}$/.test(period);
  const [showCustom, setShowCustom] = useState(isCustomRange);
  const [rangeStart, setRangeStart] = useState<string>(
    isCustomRange ? period.split("..")[0] : ""
  );
  const [rangeEnd, setRangeEnd] = useState<string>(
    isCustomRange ? period.split("..")[1] : ""
  );

  const [busy, setBusy] = useState<string | null>(null); // backendPath of in-flight export
  const [error, setError] = useState<string | null>(null);
  const [recent, setRecent] = useState<RecentExport[]>([]);

  useEffect(() => {
    setRecent(loadRecent());
  }, []);

  const setPeriodInUrl = useCallback(
    (next: string) => {
      const params = new URLSearchParams(search.toString());
      if (next === "ytd") {
        params.delete("period");
      } else {
        params.set("period", next);
      }
      const qs = params.toString();
      startTransition(() => {
        router.replace(qs ? `?${qs}` : "?", { scroll: false });
      });
    },
    [router, search]
  );

  function applyQuickPick(qp: QuickPick) {
    setShowCustom(false);
    setError(null);
    setPeriodInUrl(qp.resolve());
  }

  function applyCustomRange(start: string, end: string) {
    if (!start || !end) return;
    setError(null);
    setPeriodInUrl(`${start}..${end}`);
  }

  const activeQuickPickKey = useMemo(() => {
    for (const qp of QUICK_PICKS) {
      if (qp.resolve() === period) return qp.key;
    }
    return null;
  }, [period]);

  const downloadExport = useCallback(
    async (format: ExportFormat) => {
      const meta = FORMAT_META[format];
      setError(null);
      setBusy(meta.backendPath);
      try {
        const sp = buildExportSearchParams({ workspaceId, period, entityId });
        const filename = buildExportFilename({
          workspaceSlug,
          entitySlug: entitySlug ?? "consolidated",
          period,
          format
        });
        const res = await fetch(`/api/cpa-export/${meta.backendPath}?${sp.toString()}`);
        if (!res.ok) {
          const body = await res.text().catch(() => "");
          throw new Error(`HTTP ${res.status}${body ? ": " + body : ""}`);
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

        const next: RecentExport[] = [
          { timestamp: Date.now(), format: meta.label, filename },
          ...recent.filter((r) => r.filename !== filename)
        ].slice(0, RECENT_MAX);
        setRecent(next);
        saveRecent(next);
      } catch (e) {
        setError(e instanceof Error ? e.message : String(e));
      } finally {
        setBusy(null);
      }
    },
    [workspaceId, workspaceSlug, entityId, entitySlug, period, recent]
  );

  const csvFilename = buildExportFilename({
    workspaceSlug,
    entitySlug: entitySlug ?? "consolidated",
    period,
    format: "csv"
  });
  const iifFilename = buildExportFilename({
    workspaceSlug,
    entitySlug: entitySlug ?? "consolidated",
    period,
    format: "iif"
  });
  const qboFilename = buildExportFilename({
    workspaceSlug,
    entitySlug: entitySlug ?? "consolidated",
    period,
    format: "qbo"
  });

  return (
    <div className="space-y-6">
      {/* Period selector */}
      <Card>
        <div className="flex items-start justify-between gap-3">
          <div>
            <h2 className="text-sm font-semibold">Period</h2>
            <p className="mt-1 text-xs text-ink-tertiary">
              Active: <span className="font-mono text-ink-secondary">{period}</span> · for{" "}
              <span className="text-ink-secondary">{entityDisplayName}</span>
            </p>
          </div>
          <button
            onClick={() => setShowCustom((s) => !s)}
            className="text-xs text-ink-tertiary hover:text-ink-primary"
          >
            {showCustom ? "Hide custom range" : "Custom range"}
          </button>
        </div>

        <div className="mt-4 flex flex-wrap gap-2">
          {QUICK_PICKS.map((qp) => {
            const active = activeQuickPickKey === qp.key && !showCustom;
            return (
              <button
                key={qp.key}
                onClick={() => applyQuickPick(qp)}
                className={cn(
                  "rounded-md border px-3 py-1 text-xs font-medium transition",
                  active
                    ? "border-brand-500/40 bg-brand-500/20 text-brand-300"
                    : "border-default bg-elevated text-ink-secondary hover:text-ink-primary"
                )}
              >
                {qp.label}
              </button>
            );
          })}
        </div>

        {showCustom && (
          <div className="mt-4 flex flex-wrap items-end gap-3 border-t border-subtle pt-4">
            <label className="text-xs text-ink-secondary">
              Start
              <input
                type="date"
                value={rangeStart}
                onChange={(e) => {
                  setRangeStart(e.target.value);
                  if (e.target.value && rangeEnd) {
                    applyCustomRange(e.target.value, rangeEnd);
                  }
                }}
                className="ml-2 rounded-md border border-default bg-elevated px-2 py-1 font-mono text-xs text-ink-primary"
              />
            </label>
            <label className="text-xs text-ink-secondary">
              End
              <input
                type="date"
                value={rangeEnd}
                onChange={(e) => {
                  setRangeEnd(e.target.value);
                  if (rangeStart && e.target.value) {
                    applyCustomRange(rangeStart, e.target.value);
                  }
                }}
                className="ml-2 rounded-md border border-default bg-elevated px-2 py-1 font-mono text-xs text-ink-primary"
              />
            </label>
            <span className="text-[11px] text-ink-tertiary">
              Inclusive on both ends.
            </span>
          </div>
        )}
      </Card>

      {error && (
        <div className="rounded-md border border-debit/40 bg-debit/[0.06] px-4 py-2 text-sm text-debit">
          {error}
        </div>
      )}

      {/* 2x2 grid of export formats */}
      <div className="grid grid-cols-1 gap-4 md:grid-cols-2">
        <ExportCard
          icon={<FileSpreadsheet size={18} />}
          title="CSV"
          description="Universal spreadsheet (Excel / Numbers / Sheets)"
          filename={csvFilename}
          loading={busy === FORMAT_META.csv.backendPath}
          disabled={busy !== null}
          onDownload={() => downloadExport("csv")}
        />
        <ExportCard
          icon={<FileText size={18} />}
          title="IIF"
          description="QuickBooks Desktop import"
          filename={iifFilename}
          loading={busy === FORMAT_META.iif.backendPath}
          disabled={busy !== null}
          onDownload={() => downloadExport("iif")}
        />
        <ExportCard
          icon={<Code size={18} />}
          title="QBO XML"
          description="QuickBooks Online XML import"
          filename={qboFilename}
          loading={busy === FORMAT_META.qbo.backendPath}
          disabled={busy !== null}
          onDownload={() => downloadExport("qbo")}
        />
        <PdfReportsCard
          period={period}
          workspaceSlug={workspaceSlug}
          entitySlug={entitySlug ?? "consolidated"}
          busy={busy}
          onDownload={downloadExport}
        />
      </div>

      {recent.length > 0 && (
        <Card>
          <h2 className="text-sm font-semibold">Recent exports</h2>
          <ul className="mt-3 divide-y divide-subtle/60">
            {recent.map((r) => (
              <li key={r.timestamp + r.filename} className="flex items-center gap-3 py-2 text-xs">
                <span className="w-44 shrink-0 font-mono text-ink-tertiary">
                  {new Date(r.timestamp).toLocaleString()}
                </span>
                <span className="w-24 shrink-0 text-ink-secondary">{r.format}</span>
                <span className="truncate font-mono text-ink-secondary">{r.filename}</span>
              </li>
            ))}
          </ul>
        </Card>
      )}
    </div>
  );
}

function ExportCard({
  icon,
  title,
  description,
  filename,
  loading,
  disabled,
  onDownload
}: {
  icon: React.ReactNode;
  title: string;
  description: string;
  filename: string;
  loading: boolean;
  disabled: boolean;
  onDownload: () => void;
}) {
  return (
    <Card>
      <div className="flex items-start gap-4">
        <div className="flex h-10 w-10 shrink-0 items-center justify-center rounded-md bg-elevated text-brand-400 ring-1 ring-inset ring-brand-500/30">
          {icon}
        </div>
        <div className="flex-1 min-w-0">
          <h3 className="text-base font-semibold">{title}</h3>
          <p className="mt-1 text-sm leading-relaxed text-ink-secondary">{description}</p>
          <p className="mt-3 truncate font-mono text-[10px] text-ink-tertiary" title={filename}>
            {filename}
          </p>
          <div className="mt-4">
            <button
              onClick={onDownload}
              disabled={disabled}
              className="inline-flex items-center gap-1.5 rounded-md bg-brand-500 px-3 py-1.5 text-xs font-medium text-white shadow-brand-glow transition hover:bg-brand-600 disabled:cursor-not-allowed disabled:bg-elevated disabled:text-ink-disabled disabled:shadow-none"
            >
              {loading ? (
                <Loader2 size={13} className="animate-spin" />
              ) : (
                <Download size={13} />
              )}
              {loading ? "Preparing…" : "Download"}
            </button>
          </div>
        </div>
      </div>
    </Card>
  );
}

function PdfReportsCard({
  period,
  workspaceSlug,
  entitySlug,
  busy,
  onDownload
}: {
  period: string;
  workspaceSlug: string;
  entitySlug: string;
  busy: string | null;
  onDownload: (format: ExportFormat) => void;
}) {
  const plName = buildExportFilename({ workspaceSlug, entitySlug, period, format: "pl" });
  const bsName = buildExportFilename({
    workspaceSlug,
    entitySlug,
    period,
    format: "balance-sheet"
  });
  const cfName = buildExportFilename({
    workspaceSlug,
    entitySlug,
    period,
    format: "cash-flow"
  });
  const anyPdfBusy =
    busy === FORMAT_META.pl.backendPath ||
    busy === FORMAT_META["balance-sheet"].backendPath ||
    busy === FORMAT_META["cash-flow"].backendPath;

  return (
    <Card>
      <div className="flex items-start gap-4">
        <div className="flex h-10 w-10 shrink-0 items-center justify-center rounded-md bg-elevated text-brand-400 ring-1 ring-inset ring-brand-500/30">
          <FileType size={18} />
        </div>
        <div className="flex-1 min-w-0">
          <h3 className="text-base font-semibold">PDF Reports</h3>
          <p className="mt-1 text-sm leading-relaxed text-ink-secondary">
            Profit &amp; Loss, Balance Sheet, Cash Flow
          </p>
          <p className="mt-3 truncate font-mono text-[10px] text-ink-tertiary" title={plName}>
            {plName}
          </p>
          <div className="mt-4">
            <Dialog.Root>
              <Dialog.Trigger asChild>
                <button
                  disabled={busy !== null && !anyPdfBusy}
                  className="inline-flex items-center gap-1.5 rounded-md bg-brand-500 px-3 py-1.5 text-xs font-medium text-white shadow-brand-glow transition hover:bg-brand-600 disabled:cursor-not-allowed disabled:bg-elevated disabled:text-ink-disabled disabled:shadow-none"
                >
                  {anyPdfBusy ? (
                    <Loader2 size={13} className="animate-spin" />
                  ) : (
                    <Download size={13} />
                  )}
                  {anyPdfBusy ? "Preparing…" : "Download…"}
                </button>
              </Dialog.Trigger>
              <Dialog.Portal>
                <Dialog.Overlay className="fixed inset-0 z-50 bg-canvas/80 backdrop-blur-sm" />
                <Dialog.Content className="fixed left-1/2 top-1/2 z-50 w-[min(440px,90vw)] -translate-x-1/2 -translate-y-1/2 rounded-xl border border-default bg-surface p-6 shadow-card">
                  <div className="flex items-start justify-between gap-3">
                    <div>
                      <Dialog.Title className="text-base font-semibold">
                        Choose a PDF report
                      </Dialog.Title>
                      <Dialog.Description className="mt-1 text-xs text-ink-tertiary">
                        Each generates a single PDF for the active period and entity.
                      </Dialog.Description>
                    </div>
                    <Dialog.Close asChild>
                      <button
                        className="text-ink-tertiary hover:text-ink-primary"
                        aria-label="Close"
                      >
                        <X size={16} />
                      </button>
                    </Dialog.Close>
                  </div>
                  <div className="mt-5 space-y-2">
                    <PdfRow
                      label="Profit & Loss"
                      filename={plName}
                      loading={busy === FORMAT_META.pl.backendPath}
                      disabled={anyPdfBusy}
                      onClick={() => onDownload("pl")}
                    />
                    <PdfRow
                      label="Balance Sheet"
                      filename={bsName}
                      loading={busy === FORMAT_META["balance-sheet"].backendPath}
                      disabled={anyPdfBusy}
                      onClick={() => onDownload("balance-sheet")}
                    />
                    <PdfRow
                      label="Cash Flow"
                      filename={cfName}
                      loading={busy === FORMAT_META["cash-flow"].backendPath}
                      disabled={anyPdfBusy}
                      onClick={() => onDownload("cash-flow")}
                    />
                  </div>
                </Dialog.Content>
              </Dialog.Portal>
            </Dialog.Root>
          </div>
        </div>
      </div>
    </Card>
  );
}

function PdfRow({
  label,
  filename,
  loading,
  disabled,
  onClick
}: {
  label: string;
  filename: string;
  loading: boolean;
  disabled: boolean;
  onClick: () => void;
}) {
  return (
    <button
      onClick={onClick}
      disabled={disabled}
      className="flex w-full items-center justify-between gap-3 rounded-md border border-default bg-elevated px-3 py-2 text-left transition hover:border-strong disabled:cursor-not-allowed disabled:opacity-60"
    >
      <div className="min-w-0">
        <p className="text-sm font-medium">{label}</p>
        <p className="truncate font-mono text-[10px] text-ink-tertiary">{filename}</p>
      </div>
      {loading ? (
        <Loader2 size={14} className="shrink-0 animate-spin text-brand-400" />
      ) : (
        <Download size={14} className="shrink-0 text-ink-tertiary" />
      )}
    </button>
  );
}
