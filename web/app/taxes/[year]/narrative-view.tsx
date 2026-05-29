"use client";

// NarrativeView — headline tab for /taxes/[year].
//
// Loads the Tax Operations Coordinator's narrative summary (coordinator-summary.md)
// as the consolidated, plain-English explanation of what the agents drafted,
// what flags they raised, and what the CPA still needs to decide.
//
// Falls back to listing individual specialist drafts when the coordinator
// hasn't been run yet.

import { useEffect, useState } from "react";
import {
  AlertTriangle,
  Download,
  FileText,
  Loader2,
  Printer,
  RefreshCw,
  Sparkles,
} from "lucide-react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import { Card } from "@/components/primitives";
import { DraftMarkdownViewer } from "@/components/draft-markdown-viewer";
import { cn } from "@/lib/cn";
import type { SpecialistRun } from "@/lib/specialist-runs";

// Same prose styling pattern as draft-markdown-viewer.
const PROSE_CLASSES = cn(
  "text-sm text-ink-primary leading-relaxed",
  "[&_h1]:text-xl [&_h1]:font-bold [&_h1]:mt-0 [&_h1]:mb-4 [&_h1]:pb-2 [&_h1]:border-b [&_h1]:border-default",
  "[&_h2]:text-lg [&_h2]:font-semibold [&_h2]:mt-6 [&_h2]:mb-3 [&_h2]:pb-1 [&_h2]:border-b [&_h2]:border-subtle",
  "[&_h3]:text-base [&_h3]:font-semibold [&_h3]:mt-5 [&_h3]:mb-2",
  "[&_h4]:text-sm [&_h4]:font-medium [&_h4]:mt-4 [&_h4]:mb-1.5",
  "[&_p]:mb-3",
  "[&_ul]:mb-3 [&_ul]:pl-5 [&_ul]:list-disc",
  "[&_ol]:mb-3 [&_ol]:pl-5 [&_ol]:list-decimal",
  "[&_li]:mb-1",
  "[&_table]:w-full [&_table]:text-xs [&_table]:mb-4 [&_table]:border-collapse",
  "[&_th]:bg-elevated [&_th]:text-ink-secondary [&_th]:font-semibold [&_th]:px-2 [&_th]:py-1.5 [&_th]:text-left [&_th]:border [&_th]:border-subtle",
  "[&_td]:px-2 [&_td]:py-1 [&_td]:border [&_td]:border-subtle [&_td]:align-top",
  "[&_tr:nth-child(even)_td]:bg-canvas/50",
  "[&_code]:font-mono [&_code]:text-[11px] [&_code]:bg-elevated [&_code]:px-1 [&_code]:py-0.5 [&_code]:rounded",
  "[&_pre]:font-mono [&_pre]:text-[11px] [&_pre]:bg-elevated [&_pre]:p-3 [&_pre]:rounded-md [&_pre]:mb-3 [&_pre]:overflow-x-auto [&_pre]:border [&_pre]:border-subtle",
  "[&_pre_code]:bg-transparent [&_pre_code]:p-0",
  "[&_blockquote]:border-l-4 [&_blockquote]:border-pending [&_blockquote]:pl-4 [&_blockquote]:py-1 [&_blockquote]:bg-pending/[0.05] [&_blockquote]:text-ink-secondary [&_blockquote]:mb-3",
  "[&_strong]:font-semibold",
  "[&_em]:italic",
  "[&_hr]:border-t [&_hr]:border-default [&_hr]:my-6",
  "[&_a]:text-brand-400 [&_a]:underline [&_a]:underline-offset-2"
);

// Skeleton placeholder while content loads.
function NarrativeSkeleton() {
  return (
    <div className="space-y-4 animate-pulse" aria-label="Loading narrative">
      <div className="h-7 w-3/4 rounded bg-elevated" />
      <div className="space-y-2">
        <div className="h-3.5 w-full rounded bg-elevated/60" />
        <div className="h-3.5 w-11/12 rounded bg-elevated/60" />
        <div className="h-3.5 w-4/5 rounded bg-elevated/60" />
      </div>
      <div className="h-6 w-1/2 rounded bg-elevated mt-6" />
      <div className="space-y-2">
        <div className="h-3.5 w-full rounded bg-elevated/60" />
        <div className="h-3.5 w-10/12 rounded bg-elevated/60" />
        <div className="h-3.5 w-9/12 rounded bg-elevated/60" />
      </div>
    </div>
  );
}

const COORDINATOR_SLUG = "coordinator-summary";

// Specialist-id → display label (kept in sync with run-history-view.tsx)
const SPECIALIST_LABELS: Record<string, string> = {
  "tax-1120-preparer": "Form 1120 Preparer",
  "tax-1099-specialist": "1099-NEC Specialist",
  "tax-sc-state": "SC State Specialist",
  "tax-sc1120-specialist": "SC1120 Specialist",
  "tax-officer-comp": "Officer Compensation",
  "tax-nol-schedule-l": "NOL & Schedule L",
  "tax-audit-defense-scdor": "Audit Defense — SCDOR",
  "tax-coordinator": "Tax Operations Coordinator",
};

const SPECIALIST_SLUGS: Record<string, string> = {
  "tax-1120-preparer": "form-1120",
  "tax-1099-specialist": "1099-nec",
  "tax-sc1120-specialist": "sc1120",
  "tax-officer-comp": "officer-compensation",
  "tax-nol-schedule-l": "nol-schedule-l",
  "tax-audit-defense-scdor": "audit-defense-scdor-ftf",
  "tax-coordinator": "coordinator-summary",
};

export type NarrativeViewProps = {
  workspaceId: string;
  entityId: string;
  entityDisplayName: string;
  year: number;
  specialistRuns: SpecialistRun[];
};

// Detect DRAFT footer/header on the rendered narrative. Returns the result type
// (good | missing | indeterminate) and a tooltip. This is a permissive check —
// any of: "DRAFT — CPA REVIEW", "NOT FOR FILING", "AI-prepared", "DRAFT --"
// in the first or last ~1200 chars counts as present.
function detectDraftStamp(markdown: string): {
  status: "present" | "missing";
  detail: string;
} {
  if (!markdown) return { status: "missing", detail: "empty content" };
  // Look at head + tail windows; appendix tables can push the actual
  // closing stamp away from end-of-document.
  const head = markdown.slice(0, 1200);
  const tail = markdown.slice(-2000);

  const draftPatterns = [
    /DRAFT[\s—–-]*CPA\s*REVIEW/i,
    /NOT\s*FOR\s*FILING/i,
    /AI[\s-]*prepared/i,
    /DRAFT[\s—–-]*FOR\s*REVIEW/i,
    /CPA\s*REVIEW\s*REQUIRED/i,
  ];

  const headHit = draftPatterns.some((re) => re.test(head));
  const tailHit = draftPatterns.some((re) => re.test(tail));

  if (headHit && tailHit) {
    return { status: "present", detail: "DRAFT stamp present at head and tail" };
  }
  if (headHit || tailHit) {
    return {
      status: "present",
      detail: `DRAFT stamp present at ${headHit ? "head" : "tail"} only`,
    };
  }
  return {
    status: "missing",
    detail: "no DRAFT stamp detected — narrative may need re-running",
  };
}

export function NarrativeView({
  workspaceId,
  entityId,
  entityDisplayName,
  year,
  specialistRuns,
}: NarrativeViewProps) {
  const [markdown, setMarkdown] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [refreshing, setRefreshing] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [viewerOpen, setViewerOpen] = useState(false);
  const [viewerSlug, setViewerSlug] = useState<{
    slug: string;
    title: string;
  } | null>(null);

  // Latest completed coordinator run, if any.
  const coordinatorRun = specialistRuns
    .filter(
      (r) =>
        r.specialist_id === "tax-coordinator" && r.status === "completed"
    )
    .sort((a, b) => b.started_at.localeCompare(a.started_at))[0];

  // Map completed specialist runs (latest per specialist) for the
  // "Underlying specialist drafts" section.
  const latestRunsBySpecialist: Record<string, SpecialistRun> = {};
  for (const r of specialistRuns) {
    if (r.status !== "completed") continue;
    if (r.specialist_id === "tax-coordinator") continue;
    const existing = latestRunsBySpecialist[r.specialist_id];
    if (!existing || r.started_at > existing.started_at) {
      latestRunsBySpecialist[r.specialist_id] = r;
    }
  }

  async function fetchNarrative() {
    setError(null);
    const url =
      `/api/workspaces/${encodeURIComponent(workspaceId)}` +
      `/entities/${encodeURIComponent(entityId)}` +
      `/tax-drafts/${encodeURIComponent(year)}` +
      `/${COORDINATOR_SLUG}.md`;
    const resp = await fetch(url, { cache: "no-store" });
    if (!resp.ok) {
      let detail = `HTTP ${resp.status}`;
      try {
        const text = await resp.text();
        try {
          const j = JSON.parse(text);
          detail = j.detail ?? j.error ?? text ?? detail;
        } catch {
          detail = text || detail;
        }
      } catch {
        // keep generic detail
      }
      throw new Error(detail);
    }
    return await resp.text();
  }

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    fetchNarrative()
      .then((text) => {
        if (!cancelled) setMarkdown(text);
      })
      .catch((err) => {
        if (!cancelled) {
          setError(err instanceof Error ? err.message : "Failed to load");
          setMarkdown(null);
        }
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [workspaceId, entityId, year]);

  async function handleRefresh() {
    setRefreshing(true);
    try {
      const text = await fetchNarrative();
      setMarkdown(text);
      setError(null);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to load");
    } finally {
      setRefreshing(false);
    }
  }

  function openDraft(slug: string, title: string) {
    setViewerSlug({ slug, title });
    setViewerOpen(true);
  }

  async function downloadCoordinatorPdf() {
    const url = `/api/workspaces/${workspaceId}/entities/${entityId}/tax-drafts/${year}/${COORDINATOR_SLUG}.pdf`;
    try {
      const resp = await fetch(url, { cache: "no-store" });
      if (!resp.ok) return;
      const blob = await resp.blob();
      const link = document.createElement("a");
      link.href = URL.createObjectURL(blob);
      link.download = `coordinator-${year}-narrative.pdf`;
      link.click();
      URL.revokeObjectURL(link.href);
    } catch {
      // swallow
    }
  }

  // Detection only on rendered markdown.
  const stamp = markdown ? detectDraftStamp(markdown) : null;
  const hasCompletedSpecialists = Object.keys(latestRunsBySpecialist).length > 0;

  return (
    <div className="space-y-5">
      {/* Headline banner */}
      <div className="rounded-lg border border-brand-500/30 bg-gradient-to-r from-brand-500/[0.05] to-agent-500/[0.05] px-5 py-4">
        <div className="flex items-start gap-3">
          <Sparkles size={18} className="mt-0.5 shrink-0 text-brand-400" />
          <div className="flex-1">
            <h2 className="text-base font-semibold text-ink-primary">
              {year} narrative · {entityDisplayName}
            </h2>
            <p className="mt-1 text-xs text-ink-tertiary">
              Plain-English summary from the Tax Operations Coordinator. Reads
              each specialist&apos;s draft, consolidates flags, and explains
              what the CPA still needs to decide before filing.
            </p>
            {coordinatorRun && (
              <p className="mt-2 text-[11px] text-ink-tertiary">
                Last run by{" "}
                <span className="font-mono text-ink-secondary">
                  {coordinatorRun.model}
                </span>{" "}
                ·{" "}
                {new Date(coordinatorRun.started_at).toLocaleString()}
                {coordinatorRun.duration_ms != null && (
                  <>
                    {" "}
                    · took{" "}
                    {(coordinatorRun.duration_ms / 1000).toFixed(1)}s
                  </>
                )}
              </p>
            )}
          </div>
          <div className="flex shrink-0 items-center gap-2">
            <button
              type="button"
              onClick={handleRefresh}
              disabled={refreshing || loading}
              className="inline-flex items-center gap-1.5 rounded-md border border-default bg-elevated px-3 py-1.5 text-xs text-ink-secondary hover:text-ink-primary disabled:opacity-50"
              title="Reload from server"
            >
              <RefreshCw size={12} className={cn(refreshing && "animate-spin")} />
              Refresh
            </button>
            {markdown && (
              <>
                <button
                  type="button"
                  onClick={downloadCoordinatorPdf}
                  className="inline-flex items-center gap-1.5 rounded-md border border-default bg-elevated px-3 py-1.5 text-xs text-ink-secondary hover:text-ink-primary"
                  title="Download as PDF"
                >
                  <Download size={12} />
                  PDF
                </button>
                <button
                  type="button"
                  onClick={() => window.print()}
                  className="inline-flex items-center gap-1.5 rounded-md border border-default bg-elevated px-3 py-1.5 text-xs text-ink-secondary hover:text-ink-primary"
                  title="Print this page"
                >
                  <Printer size={12} />
                  Print
                </button>
              </>
            )}
          </div>
        </div>

        {/* Status pill row */}
        <div className="mt-3 flex flex-wrap items-center gap-2">
          {loading && !refreshing && (
            <span className="inline-flex items-center gap-1.5 rounded-full border border-default bg-elevated px-2.5 py-0.5 text-[10px] text-ink-tertiary">
              <Loader2 size={10} className="animate-spin" />
              loading
            </span>
          )}
          {stamp && (
            <span
              title={stamp.detail}
              className={cn(
                "inline-flex items-center gap-1.5 rounded-full border px-2.5 py-0.5 text-[10px] font-medium",
                stamp.status === "present"
                  ? "border-pending/40 bg-pending/[0.08] text-pending"
                  : "border-debit/40 bg-debit/[0.08] text-debit"
              )}
            >
              {stamp.status === "present" ? "DRAFT stamp present" : "DRAFT stamp missing"}
            </span>
          )}
          {markdown && (
            <span className="inline-flex items-center gap-1.5 rounded-full border border-default bg-elevated px-2.5 py-0.5 text-[10px] text-ink-tertiary">
              <FileText size={10} />
              ~{Math.ceil(markdown.length / 1024)} KB
            </span>
          )}
        </div>
      </div>

      {/* Body */}
      {loading && !markdown && (
        <Card>
          <NarrativeSkeleton />
        </Card>
      )}

      {error && !loading && (
        <Card>
          <div className="flex flex-col items-center gap-4 py-8 text-center">
            <AlertTriangle size={32} className="text-pending" />
            <div>
              <p className="text-sm font-semibold text-ink-primary">
                No coordinator narrative yet for {year}.
              </p>
              <p className="mx-auto mt-1.5 max-w-md text-xs text-ink-tertiary">
                Run the Tax Operations Coordinator to consolidate the
                specialist drafts into a single plain-English narrative.
                Either:
              </p>
              <div className="mx-auto mt-3 max-w-md rounded-md border border-default bg-elevated px-3 py-2 text-left">
                <code className="block font-mono text-[11px] text-ink-secondary">
                  make tax-coordinate \<br />
                  &nbsp;&nbsp;ENTITY={entityId.slice(0, 8)}… YEAR={year}
                </code>
              </div>
              <p className="mt-3 text-[11px] text-ink-tertiary">
                Or use the &quot;Specialist draft&quot; tab to review each
                specialist&apos;s draft individually.
              </p>
            </div>
            <button
              type="button"
              onClick={handleRefresh}
              className="inline-flex items-center gap-1.5 rounded-md border border-default bg-elevated px-3 py-1.5 text-xs text-ink-secondary hover:text-ink-primary"
            >
              <RefreshCw size={12} />
              Check again
            </button>
          </div>
        </Card>
      )}

      {markdown && !loading && (
        <Card className="px-6 py-6 md:px-10 md:py-8">
          <article className={PROSE_CLASSES}>
            <ReactMarkdown remarkPlugins={[remarkGfm]}>
              {markdown}
            </ReactMarkdown>
          </article>
        </Card>
      )}

      {/* Companion section: specialist drafts */}
      {hasCompletedSpecialists && (
        <div>
          <h3 className="mb-2 text-xs font-semibold uppercase tracking-wider text-ink-secondary">
            Underlying specialist drafts
          </h3>
          <p className="mb-3 text-[11px] text-ink-tertiary">
            The coordinator&apos;s narrative is built by reading these
            individual specialist outputs. Open any of them to inspect the
            full draft + supporting tables.
          </p>
          <div className="grid grid-cols-1 gap-2 md:grid-cols-2">
            {Object.values(latestRunsBySpecialist).map((run) => {
              const slug = SPECIALIST_SLUGS[run.specialist_id];
              if (!slug) return null;
              const label =
                SPECIALIST_LABELS[run.specialist_id] ?? run.specialist_id;
              return (
                <button
                  key={run.id}
                  type="button"
                  onClick={() => openDraft(slug, label)}
                  className="group flex items-center gap-3 rounded-lg border border-default bg-surface px-3.5 py-2.5 text-left transition hover:border-strong hover:bg-elevated/50"
                >
                  <FileText size={14} className="shrink-0 text-ink-tertiary group-hover:text-brand-400" />
                  <div className="flex-1 min-w-0">
                    <p className="truncate text-sm font-medium text-ink-primary group-hover:text-brand-300">
                      {label}
                    </p>
                    <p className="truncate text-[11px] text-ink-tertiary">
                      {new Date(run.started_at).toLocaleString()} ·{" "}
                      {run.model}
                    </p>
                  </div>
                  <span className="shrink-0 text-[10px] uppercase tracking-wider text-ink-tertiary group-hover:text-brand-400">
                    open
                  </span>
                </button>
              );
            })}
          </div>
        </div>
      )}

      {viewerSlug && (
        <DraftMarkdownViewer
          open={viewerOpen}
          onClose={() => setViewerOpen(false)}
          workspaceId={workspaceId}
          entityId={entityId}
          year={year}
          slug={viewerSlug.slug}
          title={viewerSlug.title}
        />
      )}
    </div>
  );
}
