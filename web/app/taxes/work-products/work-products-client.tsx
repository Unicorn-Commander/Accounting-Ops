"use client";

import Link from "next/link";
import { useCallback, useEffect, useMemo, useState } from "react";
import {
  ArrowRight,
  FileStack,
  FileText,
  Info,
  Loader2,
} from "lucide-react";
import { Card, EmptyState, ErrorState } from "@/components/primitives";
import { cn } from "@/lib/cn";
import {
  listWorkProducts,
  STATUS_LABEL,
  STATUS_TONE,
  fmtDate,
  type WorkProduct,
  type WorkProductStatus,
} from "@/lib/forms";

// ---------------------------------------------------------------------------
// Filters. "Certified" rolls up every non-draft status — once a form has left
// draft (self-certified / pro-reviewed / finalized) it's ready for the
// last-mile (sign, file), which is what the user cares about at a glance.
// ---------------------------------------------------------------------------

type Filter = "all" | "draft" | "certified";

const FILTERS: { id: Filter; label: string }[] = [
  { id: "all", label: "All" },
  { id: "draft", label: "Draft" },
  { id: "certified", label: "Certified" },
];

function matchesFilter(status: WorkProductStatus, filter: Filter): boolean {
  if (filter === "all") return true;
  if (filter === "draft") return status === "draft";
  // certified = anything that's no longer a draft
  return status !== "draft";
}

/** The kind code, uppercased, as a neutral chip (e.g. "1040", "SCH-C"). */
function KindChip({ kind }: { kind: string }) {
  return (
    <span className="inline-flex items-center rounded-full bg-white/5 px-2 py-0.5 text-[10px] font-medium uppercase tracking-wider text-ink-tertiary">
      {kind}
    </span>
  );
}

function StatusPill({ status }: { status: WorkProductStatus }) {
  const label = STATUS_LABEL[status] ?? status;
  const tone = STATUS_TONE[status] ?? "bg-white/5 text-ink-secondary";
  return (
    <span
      className={cn(
        "inline-flex items-center rounded-full px-2 py-0.5 text-[10px] font-medium uppercase tracking-wider",
        tone
      )}
    >
      {label}
    </span>
  );
}

function FilterChip({
  label,
  count,
  active,
  onClick,
}: {
  label: string;
  count: number;
  active: boolean;
  onClick: () => void;
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      aria-pressed={active}
      className={cn(
        "inline-flex items-center gap-1.5 rounded-full px-3 py-1 text-xs font-medium transition",
        active
          ? "bg-brand-500/15 text-brand-300 ring-1 ring-inset ring-brand-500/40"
          : "border border-default bg-surface text-ink-secondary hover:bg-elevated hover:text-ink-primary"
      )}
    >
      {label}
      <span
        className={cn(
          "tabular-nums text-[10px]",
          active ? "text-brand-300/80" : "text-ink-tertiary"
        )}
      >
        {count}
      </span>
    </button>
  );
}

function titleFor(wp: WorkProduct): string {
  return wp.title || `${wp.kind.toUpperCase()} ${wp.tax_year ?? ""}`.trim();
}

export type WorkProductsClientProps = {
  workspaceId: string;
  entityId: string;
  entitySlug: string | null;
};

export function WorkProductsClient({
  workspaceId,
  entityId,
}: WorkProductsClientProps) {
  const [items, setItems] = useState<WorkProduct[]>([]);
  const [loading, setLoading] = useState(true);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [filter, setFilter] = useState<Filter>("all");

  const load = useCallback(async () => {
    setLoading(true);
    setLoadError(null);
    try {
      const { items: list } = await listWorkProducts(workspaceId, entityId);
      setItems(list);
    } catch (e) {
      setLoadError(e instanceof Error ? e.message : String(e));
    } finally {
      setLoading(false);
    }
  }, [workspaceId, entityId]);

  useEffect(() => {
    void load();
  }, [load]);

  const counts = useMemo(() => {
    const c: Record<Filter, number> = { all: items.length, draft: 0, certified: 0 };
    for (const wp of items) {
      if (wp.status === "draft") c.draft += 1;
      else c.certified += 1;
    }
    return c;
  }, [items]);

  const filtered = useMemo(
    () => items.filter((wp) => matchesFilter(wp.status, filter)),
    [items, filter]
  );

  // ----- Loading -----------------------------------------------------------
  if (loading) {
    return (
      <div className="flex items-center gap-2 rounded-lg border border-default bg-surface px-5 py-8 text-sm text-ink-tertiary">
        <Loader2 size={15} className="animate-spin" />
        Loading your work products…
      </div>
    );
  }

  // ----- Hard load error ---------------------------------------------------
  if (loadError) {
    return (
      <div className="space-y-4">
        <IntroBanner />
        <ErrorState
          title="Couldn’t load your work products"
          description="We couldn’t reach the work-products service. Check your connection and try again."
          technicalDetails={loadError}
          recoveryAction={
            <button
              onClick={() => void load()}
              className="inline-flex items-center gap-1.5 rounded-md border border-default bg-elevated px-3 py-1.5 text-xs font-medium text-ink-secondary hover:text-ink-primary"
            >
              <Loader2 size={12} /> Try again
            </button>
          }
        />
      </div>
    );
  }

  // ----- Empty state -------------------------------------------------------
  if (items.length === 0) {
    return (
      <div className="space-y-5">
        <IntroBanner />
        <EmptyState
          icon={<FileStack size={28} className="text-ink-disabled" aria-hidden="true" />}
          title="No forms prepared yet"
          description="Prepare a form on behalf of this client — once it's filled it shows up here to review, certify, sign, and file."
          primaryAction={
            <Link
              href="/taxes/forms"
              className="inline-flex items-center gap-1.5 rounded-md bg-brand-500 px-3 py-1.5 text-xs font-medium text-white transition hover:bg-brand-600"
            >
              <FileText size={13} />
              Prepare a form
            </Link>
          }
        />
      </div>
    );
  }

  // ----- Main: filter chips + list ----------------------------------------
  return (
    <div className="space-y-6">
      <IntroBanner />

      <div className="flex flex-wrap items-center gap-1.5">
        {FILTERS.map((f) => (
          <FilterChip
            key={f.id}
            label={f.label}
            count={counts[f.id]}
            active={filter === f.id}
            onClick={() => setFilter(f.id)}
          />
        ))}
      </div>

      <Card padded={false} className="overflow-hidden">
        <div className="border-b border-subtle px-4 py-3">
          <h2 className="text-sm font-semibold">Prepared forms</h2>
          <p className="mt-0.5 text-[11px] text-ink-tertiary">
            {filtered.length}{" "}
            {filtered.length === 1 ? "work product" : "work products"}
            {filter !== "all" ? " in this view" : ""}
          </p>
        </div>

        {filtered.length === 0 ? (
          <div className="px-4 py-10 text-center text-xs text-ink-tertiary">
            No work products match this filter.
          </div>
        ) : (
          <ul className="divide-y divide-subtle/60">
            {filtered.map((wp) => (
              <li key={wp.id}>
                <Link
                  href={`/taxes/work-products/${encodeURIComponent(wp.id)}`}
                  className="group flex items-center gap-3 px-4 py-3.5 transition hover:bg-elevated/60 focus:outline-none focus-visible:bg-elevated/60"
                >
                  <span className="flex h-9 w-9 shrink-0 items-center justify-center rounded-md bg-elevated text-brand-400 ring-1 ring-inset ring-brand-500/25">
                    <FileText size={16} aria-hidden="true" />
                  </span>
                  <div className="min-w-0 flex-1">
                    <div className="flex items-center gap-2">
                      <span className="truncate text-sm font-medium text-ink-primary group-hover:text-brand-200">
                        {titleFor(wp)}
                      </span>
                      <KindChip kind={wp.kind} />
                    </div>
                    <p className="mt-0.5 text-[11px] text-ink-tertiary">
                      Tax year {wp.tax_year ?? "—"} · created{" "}
                      {fmtDate(wp.created_at)}
                    </p>
                  </div>
                  <StatusPill status={wp.status} />
                  <ArrowRight
                    size={15}
                    className="shrink-0 text-ink-tertiary transition group-hover:translate-x-0.5 group-hover:text-brand-300"
                    aria-hidden="true"
                  />
                </Link>
              </li>
            ))}
          </ul>
        )}
      </Card>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Intro banner — plain-language explainer (mirrors the W-4P LastMileGuidance).
// ---------------------------------------------------------------------------

function IntroBanner() {
  return (
    <div className="flex items-start gap-3 rounded-lg border border-brand-500/30 bg-brand-500/[0.06] px-4 py-3.5">
      <Info size={16} className="mt-0.5 shrink-0 text-brand-400" />
      <div className="min-w-0 text-xs leading-relaxed text-ink-secondary">
        <p className="font-semibold text-ink-primary">
          This is everything you&apos;ve prepared — nothing is filed until you
          say so.
        </p>
        <p className="mt-1">
          Open any form to review every line, download it, self-certify, send it
          for e-signature, or generate an e-file package. Drafts carry a DRAFT
          watermark until they&apos;re certified.
        </p>
      </div>
    </div>
  );
}
