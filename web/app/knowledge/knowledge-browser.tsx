"use client";

// KnowledgeBrowser — search + facet-filter + read view for the
// tax_knowledge_document index.

import { useEffect, useMemo, useRef, useState } from "react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import {
  BookOpen,
  ExternalLink,
  FileText,
  Filter,
  Loader2,
  Search,
  X,
} from "lucide-react";
import { Card } from "@/components/primitives";
import { cn } from "@/lib/cn";
import {
  type KnowledgeDocument,
  type KnowledgeFacets,
  type KnowledgeListResponse,
  jurisdictionLabel,
  topicLabel,
} from "@/lib/tax-knowledge";

// ---------------------------------------------------------------------------
// Prose styles (mirrors draft-markdown-viewer for consistency)
// ---------------------------------------------------------------------------

const PROSE_CLASSES = cn(
  "text-sm text-ink-primary leading-relaxed",
  "[&_h1]:text-base [&_h1]:font-bold [&_h1]:mt-4 [&_h1]:mb-2 [&_h1]:pb-1 [&_h1]:border-b [&_h1]:border-default",
  "[&_h2]:text-sm [&_h2]:font-semibold [&_h2]:mt-3 [&_h2]:mb-1.5",
  "[&_h3]:text-[13px] [&_h3]:font-semibold [&_h3]:mt-3 [&_h3]:mb-1",
  "[&_p]:mb-2.5",
  "[&_ul]:mb-2.5 [&_ul]:pl-5 [&_ul]:list-disc",
  "[&_ol]:mb-2.5 [&_ol]:pl-5 [&_ol]:list-decimal",
  "[&_li]:mb-0.5",
  "[&_table]:w-full [&_table]:text-xs [&_table]:mb-3 [&_table]:border-collapse",
  "[&_th]:bg-elevated [&_th]:text-ink-secondary [&_th]:font-semibold [&_th]:px-2 [&_th]:py-1 [&_th]:text-left [&_th]:border [&_th]:border-subtle",
  "[&_td]:px-2 [&_td]:py-1 [&_td]:border [&_td]:border-subtle [&_td]:align-top",
  "[&_code]:font-mono [&_code]:text-[11px] [&_code]:bg-elevated [&_code]:px-1 [&_code]:py-0.5 [&_code]:rounded",
  "[&_blockquote]:border-l-2 [&_blockquote]:border-pending [&_blockquote]:pl-3 [&_blockquote]:text-ink-secondary [&_blockquote]:mb-2.5",
  "[&_strong]:font-semibold",
  "[&_a]:text-brand-400 [&_a]:underline [&_a]:underline-offset-2"
);

// ---------------------------------------------------------------------------
// Doc detail panel
// ---------------------------------------------------------------------------

function DocumentDetail({
  doc,
  onClose,
}: {
  doc: KnowledgeDocument;
  onClose: () => void;
}) {
  // Close on Escape
  useEffect(() => {
    function onKey(e: KeyboardEvent) {
      if (e.key === "Escape") onClose();
    }
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, [onClose]);

  return (
    <div className="fixed inset-0 z-50 flex items-stretch justify-end">
      <button
        type="button"
        className="absolute inset-0 bg-canvas/80 backdrop-blur-sm"
        onClick={onClose}
        aria-label="Close"
      />
      <div className="relative z-10 flex h-full w-full max-w-2xl flex-col border-l border-default bg-surface shadow-card animate-fade-in">
        <div className="flex shrink-0 items-start justify-between gap-3 border-b border-subtle px-5 py-4">
          <div className="min-w-0">
            <div className="mb-1 flex flex-wrap items-center gap-1.5">
              <span className="inline-flex items-center rounded-full border border-brand-500/30 bg-brand-500/[0.08] px-2 py-0.5 font-mono text-[10px] text-brand-300">
                {doc.jurisdiction}
              </span>
              <span className="inline-flex items-center rounded-full border border-default bg-elevated px-2 py-0.5 font-mono text-[10px] text-ink-secondary">
                {topicLabel(doc.topic)}
              </span>
              {doc.tax_year && (
                <span className="inline-flex items-center rounded-full border border-default bg-elevated px-2 py-0.5 font-mono text-[10px] text-ink-secondary">
                  TY {doc.tax_year}
                </span>
              )}
              {doc.similarity != null && (
                <span className="inline-flex items-center rounded-full border border-agent-500/30 bg-agent-500/[0.08] px-2 py-0.5 font-mono text-[10px] text-agent-500">
                  match {(doc.similarity * 100).toFixed(0)}%
                </span>
              )}
            </div>
            <h2 className="truncate text-base font-semibold text-ink-primary">
              {doc.title}
            </h2>
            <p className="mt-1 text-[11px] text-ink-tertiary">
              {doc.source_authority} · v{doc.version}
              {doc.ingested_at && (
                <>
                  {" "}
                  · indexed{" "}
                  {new Date(doc.ingested_at).toLocaleDateString()}
                </>
              )}
            </p>
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
        <div className="flex-1 overflow-y-auto px-6 py-5">
          {doc.source_url && (
            <a
              href={doc.source_url}
              target="_blank"
              rel="noopener noreferrer"
              className="mb-4 inline-flex items-center gap-1.5 rounded-md border border-default bg-elevated px-3 py-1.5 text-xs text-ink-secondary hover:text-ink-primary"
            >
              <ExternalLink size={11} />
              View original source
            </a>
          )}
          {doc.content ? (
            <div className={PROSE_CLASSES}>
              <ReactMarkdown remarkPlugins={[remarkGfm]}>
                {doc.content}
              </ReactMarkdown>
            </div>
          ) : (
            <p className="text-xs text-ink-tertiary">
              No body content available.
            </p>
          )}
        </div>
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Result row
// ---------------------------------------------------------------------------

function ResultRow({
  doc,
  onClick,
}: {
  doc: KnowledgeDocument;
  onClick: () => void;
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      className="group block w-full rounded-lg border border-default bg-surface px-4 py-3 text-left transition hover:border-strong hover:bg-elevated/50"
    >
      <div className="mb-1.5 flex flex-wrap items-center gap-1.5">
        <span className="inline-flex items-center rounded-full border border-brand-500/30 bg-brand-500/[0.08] px-2 py-0.5 font-mono text-[10px] text-brand-300">
          {doc.jurisdiction}
        </span>
        <span className="inline-flex items-center rounded-full border border-default bg-elevated px-2 py-0.5 font-mono text-[10px] text-ink-tertiary">
          {topicLabel(doc.topic)}
        </span>
        {doc.tax_year && (
          <span className="font-mono text-[10px] text-ink-disabled">
            TY {doc.tax_year}
          </span>
        )}
        {doc.similarity != null && (
          <span className="ml-auto inline-flex items-center gap-1 rounded-full border border-agent-500/30 bg-agent-500/[0.08] px-2 py-0.5 font-mono text-[10px] text-agent-500">
            <span className="h-1 w-1 rounded-full bg-agent-500" />
            {(doc.similarity * 100).toFixed(0)}%
          </span>
        )}
      </div>
      <h3 className="text-sm font-medium text-ink-primary group-hover:text-brand-300">
        {doc.title}
      </h3>
      <p className="mt-1 text-[11px] text-ink-tertiary">
        {doc.source_authority} · v{doc.version}
        {doc.ingested_at && (
          <>
            {" · "}
            indexed {new Date(doc.ingested_at).toLocaleDateString()}
          </>
        )}
      </p>
    </button>
  );
}

// ---------------------------------------------------------------------------
// Main browser
// ---------------------------------------------------------------------------

export function KnowledgeBrowser({
  facets,
  initialList,
  initialQuery,
  initialJurisdiction,
  initialTopic,
  initialTaxYear,
}: {
  facets: KnowledgeFacets;
  initialList: KnowledgeListResponse | null;
  initialQuery: string;
  initialJurisdiction?: string;
  initialTopic?: string;
  initialTaxYear?: number;
}) {
  const [query, setQuery] = useState(initialQuery);
  const [jurisdiction, setJurisdiction] = useState<string | undefined>(
    initialJurisdiction
  );
  const [topic, setTopic] = useState<string | undefined>(initialTopic);
  const [taxYear, setTaxYear] = useState<number | undefined>(initialTaxYear);

  const [results, setResults] = useState<KnowledgeListResponse | null>(
    initialList
  );
  const [loading, setLoading] = useState(false);
  const [selectedDoc, setSelectedDoc] = useState<KnowledgeDocument | null>(
    null
  );
  const [detailLoading, setDetailLoading] = useState(false);

  // Debounced refetch
  const debounceRef = useRef<number | null>(null);
  useEffect(() => {
    if (debounceRef.current) {
      window.clearTimeout(debounceRef.current);
    }
    debounceRef.current = window.setTimeout(async () => {
      setLoading(true);
      try {
        const params = new URLSearchParams();
        if (query.trim()) params.set("query", query.trim());
        if (jurisdiction) params.set("jurisdiction", jurisdiction);
        if (topic) params.set("topic", topic);
        if (taxYear != null) params.set("tax_year", String(taxYear));
        params.set("limit", "30");
        const res = await fetch(
          `/api/tax-knowledge/documents?${params.toString()}`,
          { cache: "no-store" }
        );
        if (res.ok) {
          const data = (await res.json()) as KnowledgeListResponse;
          setResults(data);
        }
      } catch {
        // ignore
      } finally {
        setLoading(false);
      }
    }, 320);

    return () => {
      if (debounceRef.current) window.clearTimeout(debounceRef.current);
    };
  }, [query, jurisdiction, topic, taxYear]);

  // Open doc detail (refetch with full content)
  async function openDoc(doc: KnowledgeDocument) {
    setDetailLoading(true);
    setSelectedDoc(doc); // optimistic open
    try {
      const res = await fetch(
        `/api/tax-knowledge/documents/${encodeURIComponent(doc.id)}`,
        { cache: "no-store" }
      );
      if (res.ok) {
        const full = (await res.json()) as KnowledgeDocument;
        setSelectedDoc(full);
      }
    } catch {
      // keep optimistic version
    } finally {
      setDetailLoading(false);
    }
  }

  // Group facets-topics by jurisdiction for the sidebar
  const topicsByJurisdiction = useMemo(() => {
    const out: Record<string, KnowledgeFacets["topics"]> = {};
    for (const t of facets.topics) {
      (out[t.jurisdiction] ||= []).push(t);
    }
    return out;
  }, [facets.topics]);

  const visibleTopics = jurisdiction
    ? topicsByJurisdiction[jurisdiction] ?? []
    : facets.topics;

  const hasFilters =
    !!query.trim() || !!jurisdiction || !!topic || taxYear != null;

  function clearFilters() {
    setQuery("");
    setJurisdiction(undefined);
    setTopic(undefined);
    setTaxYear(undefined);
  }

  return (
    <div className="grid grid-cols-1 gap-5 lg:grid-cols-[260px_minmax(0,1fr)]">
      {/* Sidebar — facets */}
      <aside className="space-y-4">
        {/* Search */}
        <div className="relative">
          <Search
            size={14}
            className="pointer-events-none absolute left-3 top-1/2 -translate-y-1/2 text-ink-tertiary"
          />
          <input
            type="search"
            value={query}
            onChange={(e) => setQuery(e.target.value)}
            placeholder="Search rules, code sections, topics…"
            className="block w-full rounded-md border border-default bg-surface py-2 pl-9 pr-9 text-sm text-ink-primary placeholder-ink-disabled focus:border-brand-500 focus:outline-none focus:ring-2 focus:ring-brand-500/30"
          />
          {query && (
            <button
              type="button"
              onClick={() => setQuery("")}
              className="absolute right-2 top-1/2 -translate-y-1/2 rounded p-1 text-ink-tertiary hover:bg-white/5 hover:text-ink-primary"
              aria-label="Clear search"
            >
              <X size={13} />
            </button>
          )}
        </div>

        {/* Active-filters summary */}
        {hasFilters && (
          <div className="flex flex-wrap items-center gap-1.5 rounded-md border border-brand-500/20 bg-brand-500/[0.05] px-3 py-2 text-[11px]">
            <Filter size={11} className="shrink-0 text-brand-400" />
            <span className="text-ink-tertiary">Filters active</span>
            <button
              type="button"
              onClick={clearFilters}
              className="ml-auto text-brand-300 hover:text-brand-200"
            >
              Clear all
            </button>
          </div>
        )}

        {/* Jurisdictions */}
        <div>
          <h3 className="mb-2 text-[10px] font-semibold uppercase tracking-wider text-ink-tertiary">
            Jurisdiction
          </h3>
          <ul className="space-y-0.5">
            {facets.jurisdictions.map((j) => (
              <li key={j.jurisdiction}>
                <button
                  type="button"
                  onClick={() => {
                    if (jurisdiction === j.jurisdiction) {
                      setJurisdiction(undefined);
                      setTopic(undefined);
                    } else {
                      setJurisdiction(j.jurisdiction);
                      setTopic(undefined);
                    }
                  }}
                  className={cn(
                    "flex w-full items-center justify-between rounded px-2.5 py-1.5 text-left text-xs",
                    jurisdiction === j.jurisdiction
                      ? "bg-brand-500/[0.08] text-brand-200"
                      : "text-ink-secondary hover:bg-white/[0.04] hover:text-ink-primary"
                  )}
                >
                  <span className="truncate">
                    {jurisdictionLabel(j.jurisdiction)}
                  </span>
                  <span className="ml-2 shrink-0 font-mono text-[10px] text-ink-tertiary">
                    {j.doc_count}
                  </span>
                </button>
              </li>
            ))}
          </ul>
        </div>

        {/* Topics (filtered by jurisdiction if selected) */}
        {visibleTopics.length > 0 && (
          <div>
            <h3 className="mb-2 text-[10px] font-semibold uppercase tracking-wider text-ink-tertiary">
              Topic
              {jurisdiction && (
                <span className="ml-1 normal-case text-ink-disabled">
                  · in {jurisdiction}
                </span>
              )}
            </h3>
            <ul className="max-h-72 space-y-0.5 overflow-y-auto">
              {visibleTopics.map((t) => {
                const key = `${t.jurisdiction}:${t.topic}`;
                const active = topic === t.topic;
                return (
                  <li key={key}>
                    <button
                      type="button"
                      onClick={() => {
                        if (active) {
                          setTopic(undefined);
                        } else {
                          if (!jurisdiction) setJurisdiction(t.jurisdiction);
                          setTopic(t.topic);
                        }
                      }}
                      className={cn(
                        "flex w-full items-center justify-between rounded px-2.5 py-1.5 text-left text-xs",
                        active
                          ? "bg-brand-500/[0.08] text-brand-200"
                          : "text-ink-secondary hover:bg-white/[0.04] hover:text-ink-primary"
                      )}
                    >
                      <span className="truncate">{topicLabel(t.topic)}</span>
                      <span className="ml-2 shrink-0 font-mono text-[10px] text-ink-tertiary">
                        {t.doc_count}
                      </span>
                    </button>
                  </li>
                );
              })}
            </ul>
          </div>
        )}

        {/* Tax year */}
        {facets.tax_years.length > 0 && (
          <div>
            <h3 className="mb-2 text-[10px] font-semibold uppercase tracking-wider text-ink-tertiary">
              Tax year
            </h3>
            <div className="flex flex-wrap gap-1.5">
              {facets.tax_years.map((y) => (
                <button
                  key={y}
                  type="button"
                  onClick={() => setTaxYear(taxYear === y ? undefined : y)}
                  className={cn(
                    "rounded-md border px-2 py-0.5 font-mono text-[11px]",
                    taxYear === y
                      ? "border-brand-500 bg-brand-500/15 text-brand-200"
                      : "border-default bg-surface text-ink-secondary hover:bg-elevated"
                  )}
                >
                  {y}
                </button>
              ))}
            </div>
          </div>
        )}
      </aside>

      {/* Main pane — results */}
      <section className="min-w-0">
        <div className="mb-3 flex items-center justify-between gap-2">
          <p className="text-[11px] text-ink-tertiary">
            {loading ? (
              <span className="inline-flex items-center gap-1.5">
                <Loader2 size={11} className="animate-spin" />
                Searching…
              </span>
            ) : results ? (
              <>
                <span className="font-medium text-ink-secondary">
                  {results.total.toLocaleString()}
                </span>{" "}
                {results.total === 1 ? "document" : "documents"}
                {results.search_mode === "semantic" && (
                  <span className="ml-1.5 inline-flex items-center gap-1 rounded-full border border-agent-500/30 bg-agent-500/[0.08] px-1.5 py-0.5 text-[10px] text-agent-500">
                    semantic search
                  </span>
                )}
                {results.search_mode === "keyword" && (
                  <span className="ml-1.5 inline-flex items-center gap-1 rounded-full border border-default bg-elevated px-1.5 py-0.5 text-[10px] text-ink-tertiary">
                    keyword
                  </span>
                )}
              </>
            ) : (
              "—"
            )}
          </p>
        </div>

        {!results || results.items.length === 0 ? (
          <Card>
            <div className="flex flex-col items-center gap-3 py-10 text-center">
              <BookOpen size={32} className="text-ink-disabled" />
              <p className="text-sm font-medium text-ink-secondary">
                {hasFilters
                  ? "No documents match these filters."
                  : "Knowledge index is empty."}
              </p>
              <p className="max-w-md text-xs text-ink-tertiary">
                {hasFilters
                  ? "Try clearing a filter, broadening the search, or pick a different jurisdiction."
                  : "Run the Tax Knowledge Curator to populate the index."}
              </p>
              {hasFilters && (
                <button
                  type="button"
                  onClick={clearFilters}
                  className="mt-1 rounded-md border border-default bg-elevated px-3 py-1.5 text-xs text-ink-secondary hover:text-ink-primary"
                >
                  Clear filters
                </button>
              )}
            </div>
          </Card>
        ) : (
          <div className="space-y-2">
            {results.items.map((doc) => (
              <ResultRow
                key={doc.id}
                doc={doc}
                onClick={() => openDoc(doc)}
              />
            ))}
            {results.total > results.items.length && (
              <p className="px-3 pt-2 text-[11px] text-ink-tertiary">
                Showing {results.items.length} of{" "}
                {results.total.toLocaleString()}. Narrow the search to see
                more precise results.
              </p>
            )}
          </div>
        )}
      </section>

      {selectedDoc && (
        <DocumentDetail
          doc={selectedDoc}
          onClose={() => setSelectedDoc(null)}
        />
      )}
      {detailLoading && (
        <div className="pointer-events-none fixed inset-x-0 top-0 z-[60] h-0.5 overflow-hidden bg-elevated">
          <div className="h-full w-1/3 animate-pulse bg-brand-500" />
        </div>
      )}
    </div>
  );
}

// Suppress unused-import warning — keeping FileText for future expansion.
void FileText;
