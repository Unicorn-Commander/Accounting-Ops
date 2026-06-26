"use client";

import Link from "next/link";
import { useCallback, useEffect, useMemo, useState } from "react";
import {
  ArrowRight,
  FileStack,
  FileText,
  Info,
  Loader2,
  Search,
} from "lucide-react";
import { Card, ErrorState } from "@/components/primitives";
import { cn } from "@/lib/cn";
import * as forms from "@/lib/forms";
import {
  CATEGORY_LABEL,
  type FormCategory,
  type FormMode,
  type FormSpec,
} from "@/lib/forms";

// ---------------------------------------------------------------------------
// Display metadata.
// ---------------------------------------------------------------------------

// The order categories are rendered in (matches the catalog's mental model:
// the individual lands first, then their businesses, payroll, state, notices).
const CATEGORY_ORDER: FormCategory[] = [
  "individual",
  "business",
  "payroll",
  "state",
  "notice",
];

// Mode badge — "Computed" forms run a deterministic engine, "Fill" forms place
// your figures onto the official PDF, "Letter" forms draft correspondence.
const MODE_META: Record<FormMode, { label: string; cls: string }> = {
  computed: { label: "Computed", cls: "bg-credit/15 text-credit" },
  fill: { label: "Fill", cls: "bg-white/5 text-ink-secondary" },
  letter: { label: "Letter", cls: "bg-pending/15 text-pending" },
};

function ModeBadge({ mode }: { mode: FormMode | string }) {
  const meta = MODE_META[mode as FormMode] ?? {
    label: mode,
    cls: "bg-white/5 text-ink-secondary",
  };
  return (
    <span
      className={cn(
        "inline-flex items-center rounded-full px-2 py-0.5 text-[10px] font-medium uppercase tracking-wider",
        meta.cls
      )}
    >
      {meta.label}
    </span>
  );
}

/** Jurisdiction pill — "Federal" or the bare state code (uppercased). */
function JurisdictionBadge({ jurisdiction }: { jurisdiction: string }) {
  const isFederal = jurisdiction.toLowerCase() === "federal";
  return (
    <span className="inline-flex items-center rounded-full bg-white/5 px-2 py-0.5 text-[10px] font-medium uppercase tracking-wider text-ink-tertiary">
      {isFederal ? "Federal" : jurisdiction.toUpperCase()}
    </span>
  );
}

function categoryLabel(category: string): string {
  return CATEGORY_LABEL[category] ?? category;
}

export type FormsHubClientProps = {
  workspaceId: string;
};

export function FormsHubClient({ workspaceId }: FormsHubClientProps) {
  void workspaceId; // catalog is workspace-scoped; reserved for future filtering

  const [items, setItems] = useState<FormSpec[]>([]);
  const [loading, setLoading] = useState(true);
  const [loadError, setLoadError] = useState<string | null>(null);

  const [query, setQuery] = useState("");
  // null = "All"; otherwise a single category filter.
  const [activeCategory, setActiveCategory] = useState<string | null>(null);

  const load = useCallback(async () => {
    setLoading(true);
    setLoadError(null);
    try {
      const { items: list } = await forms.listForms();
      setItems(list);
    } catch (e) {
      setLoadError(e instanceof Error ? e.message : String(e));
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  // Categories actually present in the catalog, in canonical order, with any
  // unexpected category appended so nothing is ever silently dropped.
  const presentCategories = useMemo(() => {
    const present = new Set(items.map((f) => f.category));
    const ordered = CATEGORY_ORDER.filter((c) => present.has(c));
    const extras = [...present]
      .filter((c) => !(CATEGORY_ORDER as string[]).includes(c))
      .sort();
    return [...ordered, ...extras];
  }, [items]);

  // Apply the text + category filters.
  const filtered = useMemo(() => {
    const q = query.trim().toLowerCase();
    return items.filter((f) => {
      if (activeCategory && f.category !== activeCategory) return false;
      if (!q) return true;
      return (
        f.title.toLowerCase().includes(q) || f.kind.toLowerCase().includes(q)
      );
    });
  }, [items, query, activeCategory]);

  // Group the filtered list by category, preserving canonical order.
  const grouped = useMemo(() => {
    const byCategory = new Map<string, FormSpec[]>();
    for (const f of filtered) {
      const arr = byCategory.get(f.category);
      if (arr) arr.push(f);
      else byCategory.set(f.category, [f]);
    }
    return presentCategories
      .filter((c) => byCategory.has(c))
      .map((c) => ({ category: c, forms: byCategory.get(c) ?? [] }));
  }, [filtered, presentCategories]);

  // ----- Loading -----------------------------------------------------------
  if (loading) {
    return (
      <div className="flex items-center gap-2 rounded-lg border border-default bg-surface px-5 py-8 text-sm text-ink-tertiary">
        <Loader2 size={15} className="animate-spin" />
        Loading the tax form catalog…
      </div>
    );
  }

  // ----- Hard load error ---------------------------------------------------
  if (loadError) {
    return (
      <div className="space-y-4">
        <IntroBanner />
        <ErrorState
          title="Couldn’t load the form catalog"
          description="We couldn’t reach the forms service. Check your connection and try again."
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

  return (
    <div className="space-y-6">
      <IntroBanner />

      {/* Filter bar: search + category chips + work-products link */}
      <div className="flex flex-col gap-3 lg:flex-row lg:items-center lg:justify-between">
        <div className="flex flex-1 flex-col gap-3 sm:flex-row sm:items-center">
          <div className="relative w-full sm:max-w-xs">
            <Search
              size={14}
              className="pointer-events-none absolute left-3 top-1/2 -translate-y-1/2 text-ink-tertiary"
              aria-hidden="true"
            />
            <input
              type="search"
              value={query}
              onChange={(e) => setQuery(e.target.value)}
              placeholder="Search forms (e.g. 1040, Schedule C)…"
              aria-label="Search tax forms"
              className="w-full rounded-md border border-default bg-surface py-1.5 pl-9 pr-3 text-sm text-ink-primary placeholder:text-ink-tertiary focus:border-brand-500/60 focus:outline-none focus:ring-2 focus:ring-brand-500/30"
            />
          </div>

          {presentCategories.length > 1 && (
            <div className="flex flex-wrap items-center gap-1.5">
              <CategoryChip
                label="All"
                active={activeCategory === null}
                onClick={() => setActiveCategory(null)}
              />
              {presentCategories.map((c) => (
                <CategoryChip
                  key={c}
                  label={categoryLabel(c)}
                  active={activeCategory === c}
                  onClick={() => setActiveCategory(c)}
                />
              ))}
            </div>
          )}
        </div>

        <Link
          href="/taxes/work-products"
          className="inline-flex shrink-0 items-center gap-1.5 text-xs font-medium text-brand-400 transition hover:text-brand-300"
        >
          View prepared work products
          <ArrowRight size={13} />
        </Link>
      </div>

      {/* Empty: either the catalog is genuinely empty, or the filter hid all. */}
      {grouped.length === 0 ? (
        <Card>
          <div className="mx-auto flex max-w-md flex-col items-center gap-3 py-6 text-center">
            <span className="flex h-12 w-12 items-center justify-center rounded-full bg-elevated text-ink-disabled">
              <FileStack size={26} aria-hidden="true" />
            </span>
            <p className="text-base font-medium text-ink-primary">
              {items.length === 0
                ? "No forms available yet"
                : "No forms match your filter"}
            </p>
            <p className="text-xs leading-relaxed text-ink-tertiary">
              {items.length === 0
                ? "The form catalog is empty for now. Check back shortly — new forms are added regularly."
                : "Try a different search term or clear the category filter to see everything."}
            </p>
            {items.length > 0 && (query || activeCategory) && (
              <button
                onClick={() => {
                  setQuery("");
                  setActiveCategory(null);
                }}
                className="mt-1 inline-flex items-center gap-1.5 rounded-md border border-default bg-elevated px-3 py-1.5 text-xs font-medium text-ink-secondary transition hover:text-ink-primary"
              >
                Clear filters
              </button>
            )}
          </div>
        </Card>
      ) : (
        <div className="space-y-8">
          {grouped.map(({ category, forms: catForms }) => (
            <section key={category}>
              <div className="mb-3 flex items-baseline gap-2">
                <h2 className="text-sm font-semibold text-ink-primary">
                  {categoryLabel(category)}
                </h2>
                <span className="text-[11px] text-ink-tertiary">
                  {catForms.length}{" "}
                  {catForms.length === 1 ? "form" : "forms"}
                </span>
              </div>
              <div className="grid grid-cols-1 gap-4 md:grid-cols-2 xl:grid-cols-3">
                {catForms.map((form) => (
                  <FormCard key={form.kind} form={form} />
                ))}
              </div>
            </section>
          ))}
        </div>
      )}
    </div>
  );
}

// ---------------------------------------------------------------------------
// Form card — the whole card is a link to the prepare screen.
// ---------------------------------------------------------------------------

function FormCard({ form }: { form: FormSpec }) {
  return (
    <Card lift padded={false} className="group overflow-hidden">
      <Link
        href={`/taxes/forms/${encodeURIComponent(form.kind)}`}
        className="flex h-full flex-col gap-3 p-4 focus:outline-none focus-visible:ring-2 focus-visible:ring-brand-500/40"
        aria-label={`Prepare ${form.title}`}
      >
        <div className="flex items-start gap-3">
          <span className="flex h-9 w-9 shrink-0 items-center justify-center rounded-md bg-elevated text-brand-400 ring-1 ring-inset ring-brand-500/25">
            <FileText size={17} aria-hidden="true" />
          </span>
          <div className="min-w-0 flex-1">
            <h3 className="truncate text-sm font-semibold text-ink-primary group-hover:text-brand-200">
              {form.title}
            </h3>
            <div className="mt-1.5 flex flex-wrap items-center gap-1.5">
              <ModeBadge mode={form.mode} />
              <JurisdictionBadge jurisdiction={form.jurisdiction} />
            </div>
          </div>
        </div>

        <p className="text-[11px] leading-relaxed text-ink-tertiary">
          Goes to: {form.goes_to}
        </p>

        <div className="mt-auto flex items-center justify-end pt-1">
          <span className="inline-flex items-center gap-1.5 rounded-md bg-brand-500 px-3 py-1.5 text-sm font-medium text-white transition group-hover:bg-brand-600">
            Prepare
            <ArrowRight size={14} />
          </span>
        </div>
      </Link>
    </Card>
  );
}

// ---------------------------------------------------------------------------
// Category filter chip.
// ---------------------------------------------------------------------------

function CategoryChip({
  label,
  active,
  onClick,
}: {
  label: string;
  active: boolean;
  onClick: () => void;
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      aria-pressed={active}
      className={cn(
        "rounded-full px-3 py-1 text-xs font-medium transition",
        active
          ? "bg-brand-500/15 text-brand-300 ring-1 ring-inset ring-brand-500/40"
          : "border border-default bg-surface text-ink-secondary hover:bg-elevated hover:text-ink-primary"
      )}
    >
      {label}
    </button>
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
          Every figure is computed or sourced — nothing is filed until you say
          so.
        </p>
        <p className="mt-1">
          <strong>Computed</strong> forms (Form 1040, W-4P) calculate the figures
          with a deterministic engine. <strong>Fill</strong> forms place the
          figures you provide onto the official PDF. Nothing is filed until you
          choose to — every draft is yours to review, sign, and submit.
        </p>
      </div>
    </div>
  );
}
