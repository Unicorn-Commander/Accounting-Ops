"use client";

/**
 * DraftMarkdownViewer — centered modal that fetches and renders a specialist
 * draft Markdown source inline, via the web-layer proxy.
 *
 * Props
 * -----
 *   open         boolean — controls modal visibility
 *   onClose      () => void — called when the user closes the modal
 *   workspaceId  string
 *   entityId     string
 *   year         number
 *   slug         string — specialist slug (e.g. "form-1120")
 *   title        string — human-readable title for the modal header
 *
 * Fetch URL: /api/workspaces/{wid}/entities/{eid}/tax-drafts/{year}/{slug}.md
 *
 * Rendering: react-markdown + remark-gfm (tables, strikethrough, task lists,
 * auto-links). Styled to match the existing design-token palette.
 *
 * Print support: @media print shows only the modal body; header/footer/overlay
 * are hidden, giving clean output for paper review.
 *
 * Download button: triggers the existing {slug}.pdf route (unchanged,
 * Content-Disposition: attachment).
 *
 * The component does NOT import the DraftMarkdownViewer stylesheet separately;
 * print styles are injected via a <style> tag scoped to the viewer element.
 */

import { useEffect, useRef, useState } from "react";
import { AlertTriangle, Download, Printer, X } from "lucide-react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import { cn } from "@/lib/cn";

// ---------------------------------------------------------------------------
// Print stylesheet — injected as a <style> tag inside the modal so it only
// applies when the viewer is mounted. @media print hides everything except
// the draft content body.
// ---------------------------------------------------------------------------

const PRINT_STYLES = `
@media print {
  /* Hide everything on the page … */
  body > * { display: none !important; }
  /* … except our modal overlay (which is fixed-position) */
  [data-draft-viewer-overlay] { display: block !important; }
  [data-draft-viewer-overlay] * { display: revert !important; }
  /* But hide the modal chrome (header bar, footer, overlay backdrop) */
  [data-draft-viewer-header],
  [data-draft-viewer-footer],
  [data-draft-viewer-backdrop] {
    display: none !important;
  }
  /* The content pane should fill the page */
  [data-draft-viewer-content] {
    max-height: none !important;
    overflow: visible !important;
    padding: 12mm 15mm !important;
  }
}
`;

// ---------------------------------------------------------------------------
// Markdown prose styles (Tailwind utility-class prose equivalent, hand-tuned
// to match the existing design tokens used in the rest of the app).
// ---------------------------------------------------------------------------

const PROSE_CLASSES = cn(
  // Base
  "text-sm text-ink-primary leading-relaxed",
  // Headings
  "[&_h1]:text-lg [&_h1]:font-bold [&_h1]:mt-0 [&_h1]:mb-3 [&_h1]:pb-2 [&_h1]:border-b [&_h1]:border-default",
  "[&_h2]:text-base [&_h2]:font-semibold [&_h2]:mt-5 [&_h2]:mb-2 [&_h2]:pb-1 [&_h2]:border-b [&_h2]:border-subtle",
  "[&_h3]:text-[13px] [&_h3]:font-semibold [&_h3]:mt-4 [&_h3]:mb-1.5",
  "[&_h4]:text-sm [&_h4]:font-medium [&_h4]:mt-3 [&_h4]:mb-1",
  // Paragraphs + lists
  "[&_p]:mb-3",
  "[&_ul]:mb-3 [&_ul]:pl-5 [&_ul]:list-disc",
  "[&_ol]:mb-3 [&_ol]:pl-5 [&_ol]:list-decimal",
  "[&_li]:mb-1",
  // Tables
  "[&_table]:w-full [&_table]:text-xs [&_table]:mb-4 [&_table]:border-collapse",
  "[&_th]:bg-elevated [&_th]:text-ink-secondary [&_th]:font-semibold [&_th]:px-2 [&_th]:py-1.5 [&_th]:text-left [&_th]:border [&_th]:border-subtle",
  "[&_td]:px-2 [&_td]:py-1 [&_td]:border [&_td]:border-subtle [&_td]:align-top",
  "[&_tr:nth-child(even)_td]:bg-canvas/50",
  // Code
  "[&_code]:font-mono [&_code]:text-[11px] [&_code]:bg-elevated [&_code]:px-1 [&_code]:py-0.5 [&_code]:rounded",
  "[&_pre]:font-mono [&_pre]:text-[11px] [&_pre]:bg-elevated [&_pre]:p-3 [&_pre]:rounded-md [&_pre]:mb-3 [&_pre]:overflow-x-auto [&_pre]:border [&_pre]:border-subtle",
  "[&_pre_code]:bg-transparent [&_pre_code]:p-0",
  // Blockquotes
  "[&_blockquote]:border-l-4 [&_blockquote]:border-pending [&_blockquote]:pl-3 [&_blockquote]:py-0.5 [&_blockquote]:bg-pending/[0.05] [&_blockquote]:text-ink-secondary [&_blockquote]:mb-3",
  // Strong / em
  "[&_strong]:font-semibold [&_em]:italic",
  // Horizontal rule
  "[&_hr]:border-t [&_hr]:border-default [&_hr]:my-4",
  // Links (draft content may have inline refs)
  "[&_a]:text-brand-400 [&_a]:underline [&_a]:underline-offset-2",
);

// ---------------------------------------------------------------------------
// Props
// ---------------------------------------------------------------------------

export type DraftMarkdownViewerProps = {
  open: boolean;
  onClose: () => void;
  workspaceId: string;
  entityId: string;
  year: number;
  slug: string;
  title: string;
};

// ---------------------------------------------------------------------------
// Component
// ---------------------------------------------------------------------------

export function DraftMarkdownViewer({
  open,
  onClose,
  workspaceId,
  entityId,
  year,
  slug,
  title,
}: DraftMarkdownViewerProps) {
  const [markdown, setMarkdown] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const contentRef = useRef<HTMLDivElement>(null);

  // Fetch when modal opens or key props change.
  useEffect(() => {
    if (!open) return;
    setMarkdown(null);
    setError(null);
    setLoading(true);

    const url =
      `/api/workspaces/${encodeURIComponent(workspaceId)}` +
      `/entities/${encodeURIComponent(entityId)}` +
      `/tax-drafts/${encodeURIComponent(year)}` +
      `/${encodeURIComponent(slug)}.md`;

    fetch(url, { cache: "no-store" })
      .then(async (res) => {
        if (!res.ok) {
          let detail = `HTTP ${res.status}`;
          try {
            const json = await res.json();
            detail = json.detail ?? json.error ?? detail;
          } catch {
            // not JSON
          }
          throw new Error(detail);
        }
        return res.text();
      })
      .then((text) => {
        setMarkdown(text);
      })
      .catch((err) => {
        setError(err instanceof Error ? err.message : "Failed to load draft");
      })
      .finally(() => {
        setLoading(false);
      });
  }, [open, workspaceId, entityId, year, slug]);

  // Close on Escape key.
  useEffect(() => {
    if (!open) return;
    function onKeyDown(e: KeyboardEvent) {
      if (e.key === "Escape") onClose();
    }
    window.addEventListener("keydown", onKeyDown);
    return () => window.removeEventListener("keydown", onKeyDown);
  }, [open, onClose]);

  // Scroll lock on body while modal is open.
  useEffect(() => {
    if (open) {
      document.body.style.overflow = "hidden";
    } else {
      document.body.style.overflow = "";
    }
    return () => {
      document.body.style.overflow = "";
    };
  }, [open]);

  if (!open) return null;

  // PDF download URL — same slug, different extension.
  const pdfUrl =
    `/api/workspaces/${encodeURIComponent(workspaceId)}` +
    `/entities/${encodeURIComponent(entityId)}` +
    `/tax-drafts/${encodeURIComponent(year)}` +
    `/${encodeURIComponent(slug)}.pdf`;

  function handleDownloadPdf() {
    const a = document.createElement("a");
    a.href = pdfUrl;
    a.download = `${slug}-${year}-draft.pdf`;
    a.click();
  }

  function handlePrint() {
    window.print();
  }

  return (
    <div data-draft-viewer-overlay className="fixed inset-0 z-50 flex items-center justify-center p-4">
      {/* Inject print styles */}
      <style>{PRINT_STYLES}</style>

      {/* Backdrop */}
      <button
        data-draft-viewer-backdrop
        type="button"
        className="absolute inset-0 bg-canvas/80 backdrop-blur-sm"
        onClick={onClose}
        aria-label="Close viewer"
      />

      {/* Modal panel */}
      <div
        className={cn(
          "relative z-10 flex flex-col w-full max-w-3xl rounded-xl border border-default bg-surface shadow-card",
          "max-h-[90vh]"
        )}
        role="dialog"
        aria-modal="true"
        aria-label={title}
      >
        {/* Header */}
        <div
          data-draft-viewer-header
          className="flex shrink-0 items-center justify-between gap-3 border-b border-subtle px-5 py-3.5"
        >
          <div className="flex items-center gap-2 min-w-0">
            <span className="inline-flex items-center rounded-full bg-pending/15 px-2 py-0.5 text-[9px] font-semibold uppercase tracking-wider text-pending shrink-0">
              DRAFT
            </span>
            <h2 className="text-sm font-semibold truncate text-ink-primary">{title}</h2>
            <span className="text-[10px] text-ink-tertiary shrink-0">· {year}</span>
          </div>
          <button
            type="button"
            onClick={onClose}
            className="shrink-0 rounded p-1 text-ink-tertiary hover:bg-white/5 hover:text-ink-primary"
            aria-label="Close"
          >
            <X size={16} />
          </button>
        </div>

        {/* Body — scrollable */}
        <div
          data-draft-viewer-content
          ref={contentRef}
          className="flex-1 overflow-y-auto px-6 py-5 min-h-0"
        >
          {loading && (
            <div className="flex flex-col items-center gap-3 py-12">
              <span className="inline-block h-6 w-6 animate-spin rounded-full border-2 border-brand-500 border-t-transparent" />
              <p className="text-xs text-ink-tertiary">Loading draft…</p>
            </div>
          )}

          {error && !loading && (
            <div className="flex items-start gap-3 rounded-lg border border-debit/40 bg-debit/[0.07] px-4 py-3">
              <AlertTriangle size={15} className="mt-0.5 shrink-0 text-debit" />
              <div>
                <p className="text-sm font-semibold text-debit">Could not load draft</p>
                <p className="mt-0.5 text-xs text-debit/80">{error}</p>
                <p className="mt-1.5 text-[11px] text-ink-tertiary">
                  Run the specialist to generate a draft for {year}, then try again.
                </p>
              </div>
            </div>
          )}

          {markdown !== null && !loading && (
            <div className={PROSE_CLASSES}>
              <ReactMarkdown remarkPlugins={[remarkGfm]}>
                {markdown}
              </ReactMarkdown>
            </div>
          )}
        </div>

        {/* Footer */}
        <div
          data-draft-viewer-footer
          className="flex shrink-0 flex-wrap items-center justify-between gap-3 border-t border-subtle bg-canvas/50 px-5 py-3"
        >
          <div className="flex items-center gap-1.5 text-[10px] text-pending">
            <AlertTriangle size={11} className="shrink-0" />
            <span className="font-medium">
              DRAFT — AI-prepared. CPA review and sign-off required before filing.
            </span>
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
              onClick={handleDownloadPdf}
              className="inline-flex items-center gap-1.5 rounded-md bg-brand-500 px-3 py-1.5 text-xs font-medium text-white hover:bg-brand-600"
            >
              <Download size={12} />
              Download PDF
            </button>
          </div>
        </div>
      </div>
    </div>
  );
}
