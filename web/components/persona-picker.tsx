"use client";

import { useState } from "react";
import { ChevronDown, Cloud, Cpu, Globe, Network, Sparkles } from "lucide-react";
import { cn } from "@/lib/cn";
import {
  FALLBACK_PERSONAS,
  Persona,
  PersonaTier,
  TIER_DESCRIPTIONS,
  TIER_LABELS,
  TIER_ORDER,
  groupByTier
} from "@/lib/personas";

/** Persona picker — groups by tier, shows local-vs-cross-app badge, surfaces
 *  the "+N cross-app tools available" pill when Brigade is online and the
 *  caller has the cross-app scope.
 *
 *  Reads personas from props (the server-side fetch from /api/chat/personas
 *  passes them in). Falls back to FALLBACK_PERSONAS when the list is empty
 *  so the picker is still usable in offline dev.
 */
export function PersonaPicker({
  personas = FALLBACK_PERSONAS,
  brigadeOnline = false,
  brigadeReason = "feature_off",
  activeSlug,
  onSelect
}: {
  personas?: Persona[];
  brigadeOnline?: boolean;
  brigadeReason?: string;
  activeSlug?: string;
  onSelect?: (persona: Persona) => void;
}) {
  const [open, setOpen] = useState(false);
  const list = personas.length ? personas : FALLBACK_PERSONAS;
  const grouped = groupByTier(list);
  const active = list.find((p) => p.slug === activeSlug) ?? list[0];

  return (
    <div className="relative">
      <button
        type="button"
        onClick={() => setOpen((s) => !s)}
        aria-haspopup="listbox"
        aria-expanded={open}
        className="inline-flex items-center gap-2 rounded-md border border-default bg-elevated/70 px-3 py-1.5 text-xs font-medium text-ink-primary hover:bg-elevated"
      >
        <Sparkles size={13} className="text-agent-500" />
        <span>{active?.display_name ?? "Choose persona"}</span>
        <TierTag tier={active?.tier ?? "operational"} />
        <ChevronDown size={13} className="text-ink-tertiary" />
      </button>

      {open && (
        <div
          role="listbox"
          className="absolute right-0 z-30 mt-2 w-[28rem] max-w-[calc(100vw-2rem)] overflow-hidden rounded-lg border border-default bg-elevated shadow-ticker"
        >
          <div className="border-b border-subtle px-4 py-3">
            <div className="flex items-center justify-between gap-2">
              <p className="text-xs font-semibold text-ink-primary">
                Choose an agent persona
              </p>
              <BrigadePill online={brigadeOnline} reason={brigadeReason} />
            </div>
            <p className="mt-1 text-[11px] text-ink-tertiary">
              Personas are grouped by tier. Strategic synthesizes, specialists
              dig deep, operational keeps the lights on.
            </p>
          </div>

          <div className="max-h-[28rem] overflow-y-auto p-2">
            {TIER_ORDER.map((tier) => {
              const items = grouped[tier];
              if (!items?.length) return null;
              return (
                <section key={tier} className="mb-2 last:mb-0">
                  <header className="px-2 py-1.5">
                    <p className="text-[10px] font-semibold uppercase tracking-wider text-ink-secondary">
                      {TIER_LABELS[tier]}
                    </p>
                    <p className="text-[10px] text-ink-tertiary">
                      {TIER_DESCRIPTIONS[tier]}
                    </p>
                  </header>
                  <ul role="group" className="space-y-1">
                    {items.map((p) => (
                      <li key={p.slug}>
                        <button
                          type="button"
                          role="option"
                          aria-selected={p.slug === active?.slug}
                          onClick={() => {
                            onSelect?.(p);
                            setOpen(false);
                          }}
                          className={cn(
                            "w-full rounded-md px-3 py-2 text-left transition",
                            p.slug === active?.slug
                              ? "bg-brand-500/10 ring-1 ring-inset ring-brand-500/40"
                              : "hover:bg-white/5"
                          )}
                        >
                          <div className="flex items-center justify-between gap-3">
                            <div className="min-w-0 flex-1">
                              <p className="truncate text-sm font-medium text-ink-primary">
                                {p.display_name}
                              </p>
                              <p className="mt-0.5 line-clamp-2 text-[11px] text-ink-tertiary">
                                {p.one_line}
                              </p>
                            </div>
                            <div className="flex flex-col items-end gap-1">
                              <ReachBadge persona={p} />
                              {p.brigade_active && p.cross_app_tool_count > 0 && (
                                <CrossAppBadge count={p.cross_app_tool_count} />
                              )}
                            </div>
                          </div>
                          <div className="mt-1.5 flex flex-wrap items-center gap-1.5 text-[10px] text-ink-tertiary">
                            <span className="font-mono">{p.tool_count} tools</span>
                            {p.default_model && (
                              <>
                                <span>·</span>
                                <span className="inline-flex items-center gap-0.5">
                                  <Cpu size={9} /> {p.default_model}
                                </span>
                              </>
                            )}
                            {p.needs_tax_year && (
                              <>
                                <span>·</span>
                                <span>needs tax year</span>
                              </>
                            )}
                          </div>
                        </button>
                      </li>
                    ))}
                  </ul>
                </section>
              );
            })}
          </div>
        </div>
      )}
    </div>
  );
}

function TierTag({ tier }: { tier: PersonaTier }) {
  const map: Record<PersonaTier, string> = {
    strategic: "bg-brand-500/15 text-brand-400",
    specialist: "bg-agent-500/15 text-agent-500",
    operational: "bg-white/5 text-ink-secondary"
  };
  return (
    <span
      className={cn(
        "rounded-full px-1.5 py-0.5 text-[9px] font-medium uppercase tracking-wider",
        map[tier]
      )}
    >
      {TIER_LABELS[tier]}
    </span>
  );
}

function ReachBadge({ persona }: { persona: Persona }) {
  if (persona.consumes_brigade) {
    return (
      <span
        title="This persona can call cross-app tools when Brigade is online and the caller has brigade:cross_app scope."
        className="inline-flex items-center gap-1 rounded-md bg-brand-500/15 px-1.5 py-0.5 text-[9px] font-medium text-brand-400"
      >
        <Network size={9} />
        Cross-app
      </span>
    );
  }
  return (
    <span
      title="This persona only calls tools that live in Accounting-Ops."
      className="inline-flex items-center gap-1 rounded-md bg-white/5 px-1.5 py-0.5 text-[9px] font-medium text-ink-tertiary"
    >
      <Cpu size={9} />
      Local
    </span>
  );
}

function CrossAppBadge({ count }: { count: number }) {
  return (
    <span
      title="Cross-app tools reachable via Brigade right now for the caller's scope."
      className="inline-flex items-center gap-1 rounded-md bg-agent-500/15 px-1.5 py-0.5 text-[9px] font-medium text-agent-500"
    >
      <Globe size={9} />
      +{count} cross-app
    </span>
  );
}

function BrigadePill({ online, reason }: { online: boolean; reason: string }) {
  if (online) {
    return (
      <span
        title={`Brigade online (${reason}). Cross-app tools may be reachable.`}
        className="inline-flex items-center gap-1 rounded-full bg-agent-500/15 px-2 py-0.5 text-[10px] font-medium text-agent-500"
      >
        <Cloud size={10} />
        Brigade online
      </span>
    );
  }
  return (
    <span
      title={`Brigade offline (${reason}). Cross-app tools unavailable — local-only.`}
      className="inline-flex items-center gap-1 rounded-full bg-white/5 px-2 py-0.5 text-[10px] font-medium text-ink-tertiary"
    >
      <Cloud size={10} />
      Brigade offline
    </span>
  );
}
