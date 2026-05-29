"use client";

/**
 * NarrativeView — the "Narrative" tab in /taxes/[year].
 *
 * Renders the CPA Narrative memo (2-5 page Markdown) produced by the
 * narrative_generator agent. The narrative is Morgan's starting point —
 * it synthesises all 50-160 queue items + 5-9 specialist drafts into a
 * single CPA review letter.
 *
 * States:
 *   loading     — fetching narrative from the API on mount
 *   exists      — rendering the Markdown with a DRAFT banner
 *   not_found   — empty state with "Generate" CTA
 *   generating  — POST triggered, spinner + polling
 *   error       — error state with retry
 *
 * The "Regenerate" button calls POST /tax-narrative/{year} with
 * force_regenerate=true and then polls every 5 seconds for up to 90 seconds.
 */

import { useCallback, useEffect, useRef, useState } from "react";
import { AlertTriangle, FileText, Loader2, RefreshCw, Printer } from "lucide-react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import { cn } from "@/lib/cn";
import { Card } from "@/components/primitives";

// ---------------------------------------------------------------------------
// Prose classes — match DraftMarkdownViewer palette
// ---------------------------------------------------------------------------

const PROSE_CLASSES = cn(
  "text-sm text-ink-primary leading-relaxed",
  "[&_h1]:text-lg [&_h1]:font-bold [&_h1]:mt-0 [&_h1]:mb-3 [&_h1]:pb-2 [&_h1]:border-b [&_h1]:border-default",
  "[&_h2]:text-base [&_h2]:font-semibold [&_h2]:mt-6 [&_h2]:mb-2 [&_h2]:pb-1 [&_h2]:border-b [&_h2]:border-subtle",
  "[&_h3]:text-[13px] [&_h3]:font-semibold [&_h3]:mt-4 [&_h3]:mb-1.5",
  "[&_h4]:text-sm [&_h4]:font-medium [&_h4]:mt-3 [&_h4]:mb-1",
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
  "[&_blockquote]:border-l-4 [&_blockquote]:border-brand-400 [&_blockquote]:pl-3 [&_blockquote]:py-1 [&_blockquote]:bg-brand-500/[0.04] [&_blockquote]:text-ink-secondary [&_blockquote]:mb-3",
  "[&_strong]:font-semibold [&_em]:italic",
  "[&_hr]:border-t [&_hr]:border-default [&_hr]:my-5",
  "[&_a]:text-brand-400 [&_a]:underline [&_a]:underline-offset-2",
);

// ---------------------------------------------------------------------------
// Types
// ---------------------------------------------------------------------------

type NarrativeState =
  | { kind: "idle" }
  | { kind: "loading" }
  | { kind: "exists"; markdown: string; itemId: string | null; createdAt: string | null }
  | { kind: "not_found" }
  | { kind: "generating"; runId?: string }
  | { kind: "error"; message: string };

export type NarrativeViewProps = {
  /** Initial state fetched server-side (SSR pass). */
  initialMarkdown: string | null;
  initialItemId: string | null;
  initialCreatedAt: string | null;
  workspaceId: string;
  entityId: string;
  year: number;
};

// ---------------------------------------------------------------------------
// Component
// ---------------------------------------------------------------------------

export function NarrativeView({
  initialMarkdown,
  initialItemId,
  initialCreatedAt,
  workspaceId,
  entityId,
  year,
}: NarrativeViewProps) {
  const [state, setState] = useState<NarrativeState>(() => {
    if (initialMarkdown) {
      return {
        kind: "exists",
        markdown: initialMarkdown,
        itemId: initialItemId,
        createdAt: initialCreatedAt,
      };
    }
    return { kind: "not_found" };
  });

  // Polling ref for generation state
  const pollRef = useRef<ReturnType<typeof setInterval> | null>(null);
  const pollAttemptsRef = useRef(0);
  const MAX_POLL_ATTEMPTS = 18; // 90 seconds at 5s intervals

  const stopPolling = useCallback(() => {
    if (pollRef.current) {
      clearInterval(pollRef.current);
      pollRef.current = null;
    }
    pollAttemptsRef.current = 0;
  }, []);

  // Poll the GET endpoint until a narrative appears or we time out
  const startPolling = useCallback(() => {
    pollAttemptsRef.current = 0;
    pollRef.current = setInterval(async () => {
      pollAttemptsRef.current += 1;
      if (pollAttemptsRef.current > MAX_POLL_ATTEMPTS) {
        stopPolling();
        setState({
          kind: "error",
          message:
            "Generation timed out after 90 seconds. The narrative may still be processing — try refreshing in a moment.",
        });
        return;
      }

      try {
        const res = await fetch(
          `/api/workspaces/${workspaceId}/entities/${entityId}/tax-narrative/${year}`,
          { cache: "no-store" }
        );
        if (res.status === 404) return; // not ready yet, keep polling
        if (!res.ok) {
          stopPolling();
          setState({ kind: "error", message: `Server error ${res.status}` });
          return;
        }
        const markdown = await res.text();
        const itemId = res.headers.get("X-Narrative-Item-Id");
        const createdAt = res.headers.get("X-Narrative-Created-At");
        stopPolling();
        setState({ kind: "exists", markdown, itemId, createdAt });
      } catch {
        // Network glitch — keep polling
      }
    }, 5000);
  }, [workspaceId, entityId, year, stopPolling]);

  useEffect(() => {
    return () => stopPolling();
  }, [stopPolling]);

  // Trigger narrative generation
  async function handleGenerate(forceRegenerate = false) {
    setState({ kind: "generating" });
    stopPolling();

    try {
      const res = await fetch(
        `/api/workspaces/${workspaceId}/entities/${entityId}/tax-narrative/${year}`,
        {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ force_regenerate: forceRegenerate }),
        }
      );

      if (!res.ok) {
        let detail = `HTTP ${res.status}`;
        try {
          const data = await res.json();
          detail = data?.detail ?? data?.message ?? detail;
        } catch {
          // not JSON
        }
        setState({ kind: "error", message: detail });
        return;
      }

      const data = await res.json();

      if (data.status === "exists" && !forceRegenerate) {
        // Already exists — fetch it
        const getRes = await fetch(
          `/api/workspaces/${workspaceId}/entities/${entityId}/tax-narrative/${year}`,
          { cache: "no-store" }
        );
        if (getRes.ok) {
          const markdown = await getRes.text();
          const itemId = getRes.headers.get("X-Narrative-Item-Id");
          const createdAt = getRes.headers.get("X-Narrative-Created-At");
          setState({ kind: "exists", markdown, itemId, createdAt });
        } else {
          setState({ kind: "not_found" });
        }
        return;
      }

      // Generation started — begin polling
      setState({ kind: "generating", runId: data.run_id });
      startPolling();
    } catch {
      setState({ kind: "error", message: "Network error — please try again." });
    }
  }

  function handlePrint() {
    window.print();
  }

  // ------------------------------------------------------------------
  // Render
  // ------------------------------------------------------------------

  return (
    <div className="space-y-4">
      {/* DRAFT banner — always shown when a narrative exists */}
      {state.kind === "exists" && (
        <div className="flex flex-wrap items-center justify-between gap-3 rounded-lg border border-pending/50 bg-pending/[0.07] px-4 py-3">
          <div className="flex items-center gap-2 text-sm font-semibold text-pending">
            <AlertTriangle size={15} className="shrink-0" />
            DRAFT — AI-prepared synthesis memo. Morgan review + signature required
            before distribution.
          </div>
          <div className="flex items-center gap-2">
            <button
              type="button"
              onClick={handlePrint}
              className="inline-flex items-center gap-1.5 rounded-md border border-default bg-elevated px-3 py-1.5 text-xs text-ink-secondary hover:bg-surface hover:text-ink-primary"
            >
              <Printer size={12} />
              Print
            </button>
            <button
              type="button"
              onClick={() => handleGenerate(true)}
              className="inline-flex items-center gap-1.5 rounded-md border border-default bg-elevated px-3 py-1.5 text-xs text-ink-secondary hover:bg-surface hover:text-ink-primary"
            >
              <RefreshCw size={12} />
              Regenerate
            </button>
          </div>
        </div>
      )}

      {/* Loading skeleton */}
      {state.kind === "loading" && (
        <Card>
          <div className="flex flex-col items-center gap-3 py-12">
            <Loader2 size={28} className="animate-spin text-brand-400" />
            <p className="text-sm text-ink-secondary">Loading narrative…</p>
          </div>
        </Card>
      )}

      {/* Generation in progress */}
      {state.kind === "generating" && (
        <Card>
          <div className="flex flex-col items-center gap-4 py-12 text-center">
            <Loader2 size={32} className="animate-spin text-brand-400" />
            <div>
              <p className="text-sm font-semibold text-ink-primary">
                Generating CPA Narrative…
              </p>
              <p className="mt-1 max-w-sm text-xs text-ink-tertiary">
                The narrative generator is synthesising all queue items and specialist
                drafts. This typically takes 30–60 seconds. The page will update
                automatically when complete.
              </p>
            </div>
            {state.runId && (
              <p className="font-mono text-[10px] text-ink-disabled">
                run_id: {state.runId}
              </p>
            )}
          </div>
        </Card>
      )}

      {/* Not found / empty state */}
      {state.kind === "not_found" && (
        <Card>
          <div className="flex flex-col items-center gap-4 py-10 text-center">
            <FileText size={36} className="text-ink-disabled" />
            <div>
              <p className="text-sm font-semibold text-ink-primary">
                No narrative yet for {year}.
              </p>
              <p className="mt-1 max-w-sm text-xs text-ink-tertiary">
                The CPA Narrative synthesises all specialist drafts and queue items
                into a single review memo for Morgan. Run the specialists first, then
                generate the narrative.
              </p>
            </div>
            <button
              type="button"
              onClick={() => handleGenerate(false)}
              className={cn(
                "inline-flex items-center gap-2 rounded-md px-5 py-2.5 text-sm font-semibold",
                "bg-brand-500 text-white shadow-sm hover:bg-brand-600 active:bg-brand-700",
                "transition-colors"
              )}
            >
              <FileText size={15} />
              Generate Narrative
            </button>
          </div>
        </Card>
      )}

      {/* Error state */}
      {state.kind === "error" && (
        <Card>
          <div className="flex flex-col items-center gap-4 py-8 text-center">
            <AlertTriangle size={28} className="text-debit" />
            <div>
              <p className="text-sm font-semibold text-debit">
                Narrative generation failed
              </p>
              <p className="mt-1 max-w-sm text-xs text-debit/80">
                {state.message}
              </p>
            </div>
            <button
              type="button"
              onClick={() => handleGenerate(false)}
              className="inline-flex items-center gap-2 rounded-md border border-default bg-elevated px-4 py-2 text-sm text-ink-secondary hover:text-ink-primary"
            >
              <RefreshCw size={14} />
              Try Again
            </button>
          </div>
        </Card>
      )}

      {/* Narrative content */}
      {state.kind === "exists" && (
        <>
          {/* Metadata bar */}
          {state.createdAt && (
            <div className="flex items-center justify-between text-[11px] text-ink-tertiary">
              <span>
                Generated:{" "}
                {new Date(state.createdAt).toLocaleString("en-US", {
                  dateStyle: "medium",
                  timeStyle: "short",
                })}
              </span>
              {state.itemId && (
                <span className="font-mono">
                  item: {state.itemId.slice(0, 16)}…
                </span>
              )}
            </div>
          )}

          {/* Markdown narrative */}
          <div className="rounded-xl border border-default bg-surface p-6 shadow-sm print:border-none print:shadow-none print:p-0">
            <div className={PROSE_CLASSES}>
              <ReactMarkdown remarkPlugins={[remarkGfm]}>
                {state.markdown}
              </ReactMarkdown>
            </div>
          </div>

          {/* Regenerate CTA at bottom */}
          <div className="flex justify-end">
            <button
              type="button"
              onClick={() => handleGenerate(true)}
              className="inline-flex items-center gap-1.5 text-[11px] text-ink-tertiary hover:text-ink-primary"
            >
              <RefreshCw size={11} />
              Regenerate (force)
            </button>
          </div>
        </>
      )}
    </div>
  );
}
