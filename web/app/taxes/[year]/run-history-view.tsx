"use client";

// RunHistoryView — MLOps observability table for specialist runs.
//
// Shows a table of every specialist run for the selected entity+year:
//   started_at | specialist | model | duration | status | tokens | tool calls | output | error
//
// Click any row to expand full metrics + summaries (collapsible detail panel).
// Filters: specialist_id + status.
//
// Data is passed as initial props from the Server Component (page.tsx);
// no client-side refetch on load. A "Refresh" button triggers a manual
// re-fetch via the web-proxy GET route.

import { useCallback, useMemo, useState } from "react";
import {
  AlertCircle,
  CheckCircle2,
  ChevronDown,
  ChevronRight,
  Clock,
  FileText,
  Loader2,
  RefreshCw,
  XCircle
} from "lucide-react";
import { cn } from "@/lib/cn";
import { Card } from "@/components/primitives";
import type { SpecialistRun, SpecialistRunsResponse } from "@/lib/specialist-runs";

// ---------------------------------------------------------------------------
// Utility helpers
// ---------------------------------------------------------------------------

function formatDuration(ms: number | null): string {
  if (ms == null) return "—";
  if (ms < 1000) return `${ms}ms`;
  const s = ms / 1000;
  if (s < 60) return `${s.toFixed(1)}s`;
  const m = Math.floor(s / 60);
  const rem = Math.round(s % 60);
  return `${m}m ${rem}s`;
}

function formatTimestamp(iso: string | null): string {
  if (!iso) return "—";
  try {
    return new Intl.DateTimeFormat("en-US", {
      month: "short",
      day: "numeric",
      hour: "2-digit",
      minute: "2-digit",
      second: "2-digit",
      hour12: false
    }).format(new Date(iso));
  } catch {
    return iso;
  }
}

function formatTokens(metrics: SpecialistRun["metrics"]): string {
  if (!metrics) return "—";
  const total = (metrics.input_tokens ?? 0) + (metrics.output_tokens ?? 0);
  if (total === 0) return "—";
  const k = total / 1000;
  return k >= 1 ? `${k.toFixed(1)}k` : String(total);
}

function formatModelShort(model: string): string {
  // claude-sonnet-4-6  → sonnet-4.6
  // claude-opus-4-7    → opus-4.7
  // local-model-1-2    → local-model-1.2
  return model
    .replace(/^claude-/, "")
    .replace(/-(\d+)-(\d+)$/, "-$1.$2")
    .split("-")
    .slice(0, 2)
    .join("-");
}

function specialistLabel(id: string): string {
  const map: Record<string, string> = {
    "tax-1120-preparer": "1120 Preparer",
    "tax-1099-specialist": "1099",
    "tax-sc-state": "SC State",
    "tax-coordinator": "Coordinator"
  };
  return map[id] ?? id;
}

// ---------------------------------------------------------------------------
// Status badge
// ---------------------------------------------------------------------------

function StatusBadge({ status }: { status: string }) {
  const cfg = {
    completed: {
      icon: <CheckCircle2 size={11} />,
      cls: "border-credit/40 bg-credit/[0.08] text-credit"
    },
    running: {
      icon: <Loader2 size={11} className="animate-spin" />,
      cls: "border-brand-500/40 bg-brand-500/[0.08] text-brand-300"
    },
    failed: {
      icon: <XCircle size={11} />,
      cls: "border-debit/40 bg-debit/[0.08] text-debit"
    },
    cancelled: {
      icon: <Clock size={11} />,
      cls: "border-subtle bg-elevated text-ink-tertiary"
    }
  }[status] ?? {
    icon: <AlertCircle size={11} />,
    cls: "border-subtle bg-elevated text-ink-tertiary"
  };

  return (
    <span
      className={cn(
        "inline-flex items-center gap-1 rounded-full border px-2 py-0.5 text-[10px] font-semibold uppercase tracking-wider",
        cfg.cls
      )}
    >
      {cfg.icon}
      {status}
    </span>
  );
}

// ---------------------------------------------------------------------------
// Expanded detail panel
// ---------------------------------------------------------------------------

function RunDetailPanel({ run }: { run: SpecialistRun }) {
  return (
    <div className="border-t border-subtle/40 bg-elevated/60 px-4 py-3 text-[11px]">
      <div className="grid grid-cols-1 gap-4 sm:grid-cols-2">
        {/* Metrics */}
        {run.metrics && (
          <div>
            <p className="mb-1.5 font-semibold text-ink-secondary">Token metrics</p>
            <dl className="space-y-0.5 font-mono text-ink-tertiary">
              <div className="flex gap-2">
                <dt className="w-28 shrink-0">Input tokens</dt>
                <dd className="text-ink-primary">{run.metrics.input_tokens?.toLocaleString() ?? "—"}</dd>
              </div>
              <div className="flex gap-2">
                <dt className="w-28 shrink-0">Output tokens</dt>
                <dd className="text-ink-primary">{run.metrics.output_tokens?.toLocaleString() ?? "—"}</dd>
              </div>
              <div className="flex gap-2">
                <dt className="w-28 shrink-0">Total tools</dt>
                <dd className="text-ink-primary">{run.metrics.total_tool_calls ?? 0}</dd>
              </div>
            </dl>
            {run.metrics.mcp_tool_breakdown &&
              Object.keys(run.metrics.mcp_tool_breakdown).length > 0 && (
                <div className="mt-2">
                  <p className="mb-1 text-[10px] font-medium uppercase tracking-wider text-ink-tertiary">
                    Tool breakdown
                  </p>
                  <dl className="space-y-0.5 font-mono text-ink-tertiary">
                    {Object.entries(run.metrics.mcp_tool_breakdown).map(([name, count]) => (
                      <div key={name} className="flex gap-2">
                        <dt className="shrink-0 text-brand-300">{name}</dt>
                        <dd className="text-ink-primary">{count}×</dd>
                      </div>
                    ))}
                  </dl>
                </div>
              )}
          </div>
        )}

        {/* Output summary */}
        {run.output_summary && (
          <div>
            <p className="mb-1.5 font-semibold text-ink-secondary">Output</p>
            <dl className="space-y-0.5 font-mono text-ink-tertiary">
              {run.output_summary.output_file && (
                <div className="flex gap-2">
                  <dt className="w-24 shrink-0">File</dt>
                  <dd className="truncate text-ink-primary" title={String(run.output_summary.output_file)}>
                    {String(run.output_summary.output_file).split("/").pop()}
                  </dd>
                </div>
              )}
              {run.output_summary.output_bytes != null && (
                <div className="flex gap-2">
                  <dt className="w-24 shrink-0">Size</dt>
                  <dd className="text-ink-primary">
                    {Math.ceil(Number(run.output_summary.output_bytes) / 1024)} KB
                  </dd>
                </div>
              )}
              {run.output_summary.queue_rows_created != null && (
                <div className="flex gap-2">
                  <dt className="w-24 shrink-0">Queue rows</dt>
                  <dd className="text-ink-primary">{run.output_summary.queue_rows_created}</dd>
                </div>
              )}
            </dl>
          </div>
        )}
      </div>

      {/* Error summary */}
      {run.error_summary && (
        <div className="mt-3 rounded border border-debit/30 bg-debit/[0.06] px-3 py-2">
          <p className="mb-1 text-[10px] font-semibold uppercase tracking-wider text-debit">
            Error
          </p>
          <pre className="whitespace-pre-wrap font-mono text-[11px] text-debit/80">
            {run.error_summary}
          </pre>
        </div>
      )}

      {/* IDs */}
      <div className="mt-3 flex flex-wrap gap-x-4 gap-y-0.5 text-[10px] font-mono text-ink-disabled">
        <span>run: {run.id}</span>
        {run.trace_id && <span>trace: {run.trace_id}</span>}
        {run.invoker_uc_uid && <span>invoker: {run.invoker_uc_uid}</span>}
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Row component
// ---------------------------------------------------------------------------

function RunRow({
  run,
  expanded,
  onToggle
}: {
  run: SpecialistRun;
  expanded: boolean;
  onToggle: () => void;
}) {
  const hasDetail =
    run.metrics != null || run.output_summary != null || run.error_summary != null;

  return (
    <>
      <tr
        className={cn(
          "cursor-pointer border-b border-subtle/40 text-xs transition-colors",
          expanded ? "bg-elevated/40" : "hover:bg-elevated/30"
        )}
        onClick={hasDetail ? onToggle : undefined}
        aria-expanded={hasDetail ? expanded : undefined}
      >
        {/* Expand chevron */}
        <td className="w-6 pl-3 pr-1 py-2.5 text-ink-disabled">
          {hasDetail ? (
            expanded ? (
              <ChevronDown size={12} />
            ) : (
              <ChevronRight size={12} />
            )
          ) : null}
        </td>
        {/* Started at */}
        <td className="px-2 py-2.5 font-mono text-[11px] text-ink-tertiary tabular-nums">
          {formatTimestamp(run.started_at)}
        </td>
        {/* Specialist */}
        <td className="px-2 py-2.5 font-medium text-ink-primary">
          {specialistLabel(run.specialist_id)}
        </td>
        {/* Model */}
        <td className="px-2 py-2.5 font-mono text-[11px] text-ink-tertiary">
          {formatModelShort(run.model)}
        </td>
        {/* Duration */}
        <td className="px-2 py-2.5 tabular-nums text-ink-secondary">
          {formatDuration(run.duration_ms)}
        </td>
        {/* Status */}
        <td className="px-2 py-2.5">
          <StatusBadge status={run.status} />
        </td>
        {/* Tokens */}
        <td className="px-2 py-2.5 font-mono text-[11px] tabular-nums text-ink-secondary">
          {formatTokens(run.metrics)}
        </td>
        {/* Tool calls */}
        <td className="px-2 py-2.5 tabular-nums text-ink-secondary">
          {run.metrics?.total_tool_calls ?? "—"}
        </td>
        {/* Output file */}
        <td className="px-2 py-2.5 text-ink-tertiary">
          {run.output_summary?.output_file ? (
            <span className="inline-flex items-center gap-1">
              <FileText size={11} />
              <span className="max-w-[120px] truncate font-mono text-[10px]" title={String(run.output_summary.output_file)}>
                {String(run.output_summary.output_file).split("/").pop()}
              </span>
            </span>
          ) : (
            "—"
          )}
        </td>
        {/* Error indicator */}
        <td className="px-2 py-2.5 pr-3">
          {run.error_summary ? (
            <span className="truncate max-w-[160px] block text-[10px] text-debit" title={run.error_summary}>
              {run.error_summary.split("\n")[0]}
            </span>
          ) : null}
        </td>
      </tr>
      {expanded && hasDetail && (
        <tr>
          <td colSpan={10} className="p-0">
            <RunDetailPanel run={run} />
          </td>
        </tr>
      )}
    </>
  );
}

// ---------------------------------------------------------------------------
// Filter bar
// ---------------------------------------------------------------------------

const SPECIALIST_OPTIONS = [
  { value: "", label: "All specialists" },
  { value: "tax-1120-preparer", label: "1120 Preparer" },
  { value: "tax-1099-specialist", label: "1099" },
  { value: "tax-sc-state", label: "SC State" },
  { value: "tax-coordinator", label: "Coordinator" }
];

const STATUS_OPTIONS = [
  { value: "all", label: "All statuses" },
  { value: "completed", label: "Completed" },
  { value: "failed", label: "Failed" },
  { value: "running", label: "Running" },
  { value: "cancelled", label: "Cancelled" }
];

// ---------------------------------------------------------------------------
// Main component
// ---------------------------------------------------------------------------

export type RunHistoryViewProps = {
  initialRuns: SpecialistRun[];
  workspaceId: string;
  entityId: string;
  year: number;
};

export function RunHistoryView({
  initialRuns,
  workspaceId,
  entityId,
  year
}: RunHistoryViewProps) {
  const [runs, setRuns] = useState<SpecialistRun[]>(initialRuns);
  const [expandedIds, setExpandedIds] = useState<Set<string>>(new Set());
  const [specialistFilter, setSpecialistFilter] = useState("");
  const [statusFilter, setStatusFilter] = useState("all");
  const [refreshing, setRefreshing] = useState(false);

  const toggleExpand = useCallback((id: string) => {
    setExpandedIds((prev) => {
      const next = new Set(prev);
      if (next.has(id)) {
        next.delete(id);
      } else {
        next.add(id);
      }
      return next;
    });
  }, []);

  const handleRefresh = useCallback(async () => {
    setRefreshing(true);
    try {
      const params = new URLSearchParams({
        entity_id: entityId,
        tax_year: String(year)
      });
      if (specialistFilter) params.set("specialist_id", specialistFilter);
      if (statusFilter && statusFilter !== "all") params.set("status", statusFilter);

      const res = await fetch(
        `/api/workspaces/${workspaceId}/specialist-runs?${params}`,
        { cache: "no-store" }
      );
      if (res.ok) {
        const data = (await res.json()) as SpecialistRunsResponse;
        setRuns(data.items);
      }
    } catch {
      // ignore refresh failures
    } finally {
      setRefreshing(false);
    }
  }, [workspaceId, entityId, year, specialistFilter, statusFilter]);

  const visible = useMemo(() => {
    return runs.filter((r) => {
      if (specialistFilter && r.specialist_id !== specialistFilter) return false;
      if (statusFilter && statusFilter !== "all" && r.status !== statusFilter) return false;
      return true;
    });
  }, [runs, specialistFilter, statusFilter]);

  const hasRunning = runs.some((r) => r.status === "running");

  return (
    <div className="space-y-4">
      {/* Info banner for active runs */}
      {hasRunning && (
        <div className="flex items-center gap-2 rounded-lg border border-brand-500/30 bg-brand-500/[0.06] px-4 py-2.5 text-xs text-brand-300">
          <Loader2 size={13} className="animate-spin shrink-0" />
          <span>
            {runs.filter((r) => r.status === "running").length} run(s) currently in
            progress. Refresh to see latest status.
          </span>
        </div>
      )}

      {/* Filter + refresh bar */}
      <div className="flex flex-wrap items-center gap-2">
        <select
          value={specialistFilter}
          onChange={(e) => setSpecialistFilter(e.target.value)}
          className="rounded-md border border-default bg-elevated px-2.5 py-1.5 text-xs text-ink-primary focus:border-brand-500 focus:outline-none"
        >
          {SPECIALIST_OPTIONS.map((o) => (
            <option key={o.value} value={o.value}>
              {o.label}
            </option>
          ))}
        </select>

        <select
          value={statusFilter}
          onChange={(e) => setStatusFilter(e.target.value)}
          className="rounded-md border border-default bg-elevated px-2.5 py-1.5 text-xs text-ink-primary focus:border-brand-500 focus:outline-none"
        >
          {STATUS_OPTIONS.map((o) => (
            <option key={o.value} value={o.value}>
              {o.label}
            </option>
          ))}
        </select>

        <button
          type="button"
          onClick={handleRefresh}
          disabled={refreshing}
          className="ml-auto inline-flex items-center gap-1.5 rounded-md border border-default bg-elevated px-3 py-1.5 text-xs text-ink-secondary hover:text-ink-primary disabled:cursor-not-allowed disabled:opacity-50"
        >
          <RefreshCw size={12} className={cn(refreshing && "animate-spin")} />
          Refresh
        </button>
      </div>

      {/* Table */}
      {visible.length === 0 ? (
        <Card>
          <div className="flex flex-col items-center gap-3 py-8 text-center">
            <Clock size={32} className="text-ink-disabled" />
            <p className="text-sm font-medium text-ink-secondary">
              No runs recorded yet for {year}.
            </p>
            <p className="max-w-sm text-xs text-ink-tertiary">
              When a specialist runs, its metadata (model, tokens, duration,
              output) will appear here automatically.
            </p>
          </div>
        </Card>
      ) : (
        <div className="overflow-x-auto rounded-lg border border-default">
          <table className="w-full min-w-[800px]">
            <thead>
              <tr className="border-b border-subtle bg-elevated/60 text-[10px] font-medium uppercase tracking-wider text-ink-tertiary">
                <th className="w-6 pl-3 pr-1 py-2" />
                <th className="px-2 py-2 text-left">Started</th>
                <th className="px-2 py-2 text-left">Specialist</th>
                <th className="px-2 py-2 text-left">Model</th>
                <th className="px-2 py-2 text-left">Duration</th>
                <th className="px-2 py-2 text-left">Status</th>
                <th className="px-2 py-2 text-left">Tokens</th>
                <th className="px-2 py-2 text-left">Tools</th>
                <th className="px-2 py-2 text-left">Output</th>
                <th className="px-2 py-2 pr-3 text-left">Error</th>
              </tr>
            </thead>
            <tbody>
              {visible.map((run) => (
                <RunRow
                  key={run.id}
                  run={run}
                  expanded={expandedIds.has(run.id)}
                  onToggle={() => toggleExpand(run.id)}
                />
              ))}
            </tbody>
          </table>
          {visible.length < runs.length && (
            <p className="border-t border-subtle px-4 py-2 text-[11px] text-ink-tertiary">
              Showing {visible.length} of {runs.length} runs (filters applied).
            </p>
          )}
        </div>
      )}
    </div>
  );
}
