"use client";

import { ChevronDown, Sparkles, Star } from "lucide-react";
import { useEffect, useMemo, useRef, useState } from "react";
import { cn } from "@/lib/cn";
import type { ChatPersona } from "./types";

// Personas pinned at the top under "Recommended" — keep this list short and
// stable; these are the entry points 95% of users want.
const RECOMMENDED_SLUGS = ["cpa-tax-resolver", "setup-assistant"] as const;

// Tier order shown after "Recommended". Mirrors agents/chat/persona_registry.py.
const TIER_ORDER = ["strategic", "operational", "specialist"] as const;
type TierKey = (typeof TIER_ORDER)[number];

const TIER_LABEL: Record<TierKey, string> = {
  strategic: "Strategic",
  operational: "Operational",
  specialist: "Specialist",
};

// Optional per-persona display tier override. The Python registry already
// stores tier on every persona, but ChatPersona (web type) historically only
// carried slug+display_name+one_line. If the wire payload includes a tier we
// use it; otherwise we fall back to this map.
const TIER_FALLBACK: Record<string, TierKey> = {
  "cfo-agent": "strategic",
  "cpa-tax-resolver": "strategic",
  "setup-assistant": "operational",
  "form-1120-preparer": "specialist",
  "sc-state-specialist": "specialist",
  "state-strategy-drafter": "specialist",
  "federal-strategy-drafter": "specialist",
  "1099-specialist": "specialist",
  "officer-compensation-specialist": "specialist",
  "nol-schedule-l-analyst": "specialist",
  "audit-defense-specialist": "specialist",
  "personal-1040-specialist": "specialist",
};

function personaTier(p: ChatPersona): TierKey {
  // The wire shape may or may not include a `tier` field. Treat it as optional.
  const t = (p as { tier?: string }).tier;
  if (t && (TIER_ORDER as readonly string[]).includes(t)) {
    return t as TierKey;
  }
  return TIER_FALLBACK[p.slug] ?? "specialist";
}

/**
 * Grouped persona picker:
 *   - Pins CPA Tax Resolver + Setup Assistant at the top as "Recommended".
 *   - Then groups the remainder by tier (Strategic / Operational / Specialist).
 *   - Adds a "Not sure? Ask the default agent" footer hint.
 *   - Honors DEFAULT_PERSONA_SLUG: the `value` prop wins; we never override it.
 */
export function PersonaPicker({
  personas,
  value,
  onChange,
  disabled = false,
  compact = false,
}: {
  personas: ChatPersona[];
  value: string;
  onChange: (slug: string) => void;
  disabled?: boolean;
  compact?: boolean;
}) {
  const [open, setOpen] = useState(false);
  const ref = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (!open) return;
    function onDoc(e: MouseEvent) {
      if (ref.current && !ref.current.contains(e.target as Node)) setOpen(false);
    }
    function onKey(e: KeyboardEvent) {
      if (e.key === "Escape") setOpen(false);
    }
    document.addEventListener("mousedown", onDoc);
    document.addEventListener("keydown", onKey);
    return () => {
      document.removeEventListener("mousedown", onDoc);
      document.removeEventListener("keydown", onKey);
    };
  }, [open]);

  const active = personas.find((p) => p.slug === value) ?? personas[0];

  // Group personas — Recommended first (preserving RECOMMENDED_SLUGS order),
  // then by tier. A persona that's "Recommended" is omitted from its tier
  // group so it doesn't appear twice.
  const { recommended, tiers } = useMemo(() => {
    const recommendedSet = new Set<string>(RECOMMENDED_SLUGS);
    const recommended: ChatPersona[] = [];
    for (const slug of RECOMMENDED_SLUGS) {
      const p = personas.find((x) => x.slug === slug);
      if (p) recommended.push(p);
    }
    const tiers: Record<TierKey, ChatPersona[]> = {
      strategic: [],
      operational: [],
      specialist: [],
    };
    for (const p of personas) {
      if (recommendedSet.has(p.slug)) continue;
      tiers[personaTier(p)].push(p);
    }
    return { recommended, tiers };
  }, [personas]);

  // Default-agent hint: same default the page-level Server Component uses.
  // We don't try to detect it; the wrapper passes `value` initialized to it.
  const defaultSlug = RECOMMENDED_SLUGS[0]; // CPA Tax Resolver

  return (
    <div ref={ref} className="relative">
      <button
        type="button"
        onClick={() => !disabled && setOpen((o) => !o)}
        disabled={disabled}
        aria-haspopup="listbox"
        aria-expanded={open}
        aria-label={`Agent persona: ${active?.display_name ?? "pick one"}`}
        className={cn(
          "inline-flex items-center gap-1.5 rounded-md border border-default bg-elevated/60",
          "px-2.5 py-1.5 text-xs font-medium text-ink-primary hover:bg-elevated",
          "disabled:cursor-not-allowed disabled:opacity-60",
          compact && "py-1"
        )}
      >
        <Sparkles size={12} className="text-agent-500" />
        <span className="truncate max-w-[12rem]">
          {active?.display_name ?? "(pick agent)"}
        </span>
        <ChevronDown size={12} className="text-ink-tertiary" />
      </button>
      {open && (
        <div
          role="listbox"
          aria-label="Agent persona"
          className="absolute left-0 z-30 mt-1 w-[26rem] max-w-[calc(100vw-1rem)] overflow-hidden rounded-md border border-default bg-surface shadow-card"
        >
          <div className="max-h-[68vh] overflow-y-auto">
            {recommended.length > 0 && (
              <PersonaGroup
                title="Recommended"
                titleAccent="brand"
                icon={<Star size={10} className="text-brand-300" />}
                personas={recommended}
                value={value}
                onPick={(slug) => {
                  onChange(slug);
                  setOpen(false);
                }}
              />
            )}
            {TIER_ORDER.map((tier) => {
              if (tiers[tier].length === 0) return null;
              return (
                <PersonaGroup
                  key={tier}
                  title={TIER_LABEL[tier]}
                  titleAccent="ink"
                  personas={tiers[tier]}
                  value={value}
                  onPick={(slug) => {
                    onChange(slug);
                    setOpen(false);
                  }}
                />
              );
            })}
            {/* Default-agent hint */}
            <div className="border-t border-subtle px-3 py-2 text-[10px] text-ink-tertiary">
              Not sure?{" "}
              <button
                type="button"
                onClick={() => {
                  onChange(defaultSlug);
                  setOpen(false);
                }}
                className="font-medium text-brand-400 underline-offset-2 hover:underline focus:outline-none focus-visible:underline"
              >
                Ask the default agent
              </button>{" "}
              (CPA Tax Resolver).
            </div>
          </div>
        </div>
      )}
    </div>
  );
}

function PersonaGroup({
  title,
  titleAccent,
  icon,
  personas,
  value,
  onPick,
}: {
  title: string;
  titleAccent: "brand" | "ink";
  icon?: React.ReactNode;
  personas: ChatPersona[];
  value: string;
  onPick: (slug: string) => void;
}) {
  return (
    <div className="border-b border-subtle/60 last:border-b-0">
      <div
        className={cn(
          "flex items-center gap-1.5 px-3 py-1.5 text-[10px] font-semibold uppercase tracking-wider",
          titleAccent === "brand" ? "text-brand-300" : "text-ink-tertiary"
        )}
      >
        {icon}
        <span>{title}</span>
      </div>
      <ul className="py-1">
        {personas.map((p) => {
          const selected = p.slug === value;
          return (
            <li key={p.slug}>
              <button
                type="button"
                onClick={() => onPick(p.slug)}
                className={cn(
                  "block w-full px-3 py-2 text-left text-xs hover:bg-white/[0.05] focus:bg-white/[0.06] focus:outline-none",
                  selected && "bg-brand-500/[0.08]"
                )}
                role="option"
                aria-selected={selected}
              >
                <div className="flex items-center gap-2">
                  <Sparkles
                    size={11}
                    className={selected ? "text-brand-300" : "text-agent-500/70"}
                  />
                  <p className={cn("font-medium", selected ? "text-brand-200" : "text-ink-primary")}>
                    {p.display_name}
                  </p>
                  {selected && (
                    <span className="ml-auto text-[10px] text-brand-300">●</span>
                  )}
                </div>
                <p className="mt-0.5 pl-5 text-[10px] text-ink-tertiary">
                  {p.one_line}
                </p>
              </button>
            </li>
          );
        })}
      </ul>
    </div>
  );
}
