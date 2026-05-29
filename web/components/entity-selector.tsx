"use client";

import { useRouter } from "next/navigation";
import { useEffect, useRef, useState, useTransition } from "react";
import { Building2, ChevronDown, Layers, ShieldCheck, User } from "lucide-react";
import { cn } from "@/lib/cn";
import type { Entity } from "@/lib/api";

const KIND_LABEL: Record<string, string> = {
  c_corp: "C-Corp",
  s_corp: "S-Corp",
  llc: "LLC",
  partnership: "Partnership",
  sole_proprietorship: "Sole Prop",
  personal: "Personal",
  trust: "Trust",
  non_profit: "Non-profit",
  estate: "Estate",
  other: "Other"
};

const CONSOLIDATED_ID = "consolidated";

export function EntitySelector({
  entities,
  activeEntityId,
  showConsolidated = true,
  canConsolidate = true,
  size = "md"
}: {
  entities: Entity[];
  activeEntityId?: string;
  showConsolidated?: boolean;
  canConsolidate?: boolean;
  size?: "sm" | "md";
}) {
  const router = useRouter();
  const [open, setOpen] = useState(false);
  const [, startTransition] = useTransition();
  const rootRef = useRef<HTMLDivElement>(null);

  // Close on outside click + Escape
  useEffect(() => {
    if (!open) return;
    function onClick(e: MouseEvent) {
      if (rootRef.current && !rootRef.current.contains(e.target as Node)) {
        setOpen(false);
      }
    }
    function onKey(e: KeyboardEvent) {
      if (e.key === "Escape") setOpen(false);
    }
    document.addEventListener("mousedown", onClick);
    document.addEventListener("keydown", onKey);
    return () => {
      document.removeEventListener("mousedown", onClick);
      document.removeEventListener("keydown", onKey);
    };
  }, [open]);

  if (entities.length === 0) {
    return (
      <div
        className={cn(
          "rounded-md border border-pending/40 bg-pending/[0.06] text-pending",
          size === "sm" ? "px-2 py-1 text-xs" : "px-3 py-1.5 text-sm"
        )}
      >
        No entities · provision in workspace
      </div>
    );
  }

  const consolidated = canConsolidate && showConsolidated
    ? {
        id: CONSOLIDATED_ID,
        display_name: "All entities (consolidated)",
        kind: "workspace"
      }
    : null;

  // Default-entity policy: prefer first non-personal entity for visual fallback
  // when no activeEntityId is set. (See lib/active-context.ts for the canonical
  // server-side resolution.)
  // Same kind-priority ordering as web/lib/active-context.ts.  Keeps the
  // "what entity am I implicitly on?" answer consistent between the server's
  // default and this client-side fallback display.
  const _KIND_PRIORITY: Record<string, number> = {
    c_corp: 0, s_corp: 1, sole_prop: 2, llc: 3,
    partnership: 4, trust: 5, estate: 6, personal: 7,
  };
  const defaultEntity =
    [...entities].sort((a, b) => {
      const ra = a.kind in _KIND_PRIORITY ? _KIND_PRIORITY[a.kind] : 99;
      const rb = b.kind in _KIND_PRIORITY ? _KIND_PRIORITY[b.kind] : 99;
      return ra !== rb ? ra - rb : a.display_name.localeCompare(b.display_name);
    })[0] ?? entities[0];

  const active =
    entities.find((e) => e.id === activeEntityId) ??
    (activeEntityId === CONSOLIDATED_ID && consolidated
      ? { ...consolidated, slug: CONSOLIDATED_ID, legal_name: null, country_code: "US", functional_currency: "USD", hipaa_protected: false, status: "active", created_at: "" }
      : defaultEntity);

  function selectEntity(entityId: string) {
    document.cookie = `aops_entity=${entityId}; Path=/; SameSite=Lax; Max-Age=2592000`;
    setOpen(false);
    startTransition(() => router.refresh());
  }

  const isPersonal = "kind" in active && active.kind === "personal";
  const isConsolidated = active.id === CONSOLIDATED_ID;

  return (
    <div className="relative" ref={rootRef}>
      <button
        onClick={() => setOpen(!open)}
        aria-haspopup="listbox"
        aria-expanded={open}
        aria-label={`Active entity: ${active.display_name}. Click to change.`}
        className={cn(
          "flex items-center gap-2 rounded-md border bg-surface transition focus:outline-none focus:ring-2 focus:ring-brand-500/40",
          open
            ? "border-brand-500/60 bg-elevated"
            : "border-default hover:bg-elevated",
          size === "sm" ? "px-2.5 py-1 text-xs" : "px-3 py-1.5 text-sm"
        )}
      >
        {isConsolidated ? (
          <Layers
            size={size === "sm" ? 12 : 14}
            className="text-brand-400"
          />
        ) : isPersonal ? (
          <User size={size === "sm" ? 12 : 14} className="text-ink-tertiary" />
        ) : (
          <Building2 size={size === "sm" ? 12 : 14} className="text-agent-500" />
        )}
        <span className="font-medium max-w-[200px] truncate">{active.display_name}</span>
        <ChevronDown
          size={size === "sm" ? 11 : 13}
          className={cn(
            "text-ink-tertiary transition-transform",
            open && "rotate-180"
          )}
        />
      </button>

      {open && (
        <div
          role="listbox"
          aria-label="Entity"
          className="absolute right-0 z-40 mt-1.5 w-80 rounded-lg border border-default bg-elevated shadow-card animate-fade-in"
        >
          <div className="border-b border-subtle px-3 py-2 flex items-center justify-between">
            <p className="text-[10px] font-medium uppercase tracking-wider text-ink-tertiary">
              Entity ({entities.length})
            </p>
            <p className="text-[10px] text-ink-disabled">click to switch</p>
          </div>
          <ul className="max-h-80 overflow-y-auto py-1">
            {entities.map((entity) => {
              const isActive = entity.id === active.id;
              const entityIsPersonal = entity.kind === "personal";
              return (
                <li key={entity.id}>
                  <button
                    onClick={() => selectEntity(entity.id)}
                    aria-selected={isActive}
                    className={cn(
                      "flex w-full items-center gap-3 px-3 py-2 text-left text-sm transition hover:bg-white/[0.04] focus:outline-none focus:bg-white/[0.06]",
                      isActive && "bg-brand-500/[0.08]"
                    )}
                  >
                    {entityIsPersonal ? (
                      <User
                        size={13}
                        className={isActive ? "text-brand-400" : "text-ink-tertiary"}
                      />
                    ) : (
                      <Building2
                        size={13}
                        className={isActive ? "text-brand-400" : "text-agent-500/80"}
                      />
                    )}
                    <div className="flex-1 min-w-0">
                      <div className="flex items-center gap-2">
                        <span className={cn("font-medium truncate", isActive && "text-brand-200")}>
                          {entity.display_name}
                        </span>
                        {entity.hipaa_protected && (
                          <ShieldCheck
                            size={11}
                            className="text-agent-500 shrink-0"
                            aria-label="HIPAA-protected"
                          />
                        )}
                      </div>
                      <div className="text-[10px] uppercase tracking-wider text-ink-tertiary">
                        {KIND_LABEL[entity.kind] ?? entity.kind}
                        {entity.functional_currency !== "USD" &&
                          ` · ${entity.functional_currency}`}
                      </div>
                    </div>
                    {isActive && (
                      <span className="text-[10px] text-brand-400">●</span>
                    )}
                  </button>
                </li>
              );
            })}
          </ul>
          {consolidated && (
            <>
              <div className="border-t border-subtle" />
              <button
                onClick={() => selectEntity(CONSOLIDATED_ID)}
                aria-selected={active.id === CONSOLIDATED_ID}
                className={cn(
                  "flex w-full items-center gap-3 px-3 py-2 text-left text-sm transition hover:bg-white/[0.04] focus:outline-none focus:bg-white/[0.06]",
                  active.id === CONSOLIDATED_ID && "bg-brand-500/[0.08]"
                )}
              >
                <Layers
                  size={13}
                  className={
                    active.id === CONSOLIDATED_ID
                      ? "text-brand-400"
                      : "text-ink-tertiary"
                  }
                />
                <div className="flex-1">
                  <div className={cn("font-medium", active.id === CONSOLIDATED_ID && "text-brand-200")}>
                    All entities (consolidated)
                  </div>
                  <div className="text-[10px] uppercase tracking-wider text-ink-tertiary">
                    Owner / admin only
                  </div>
                </div>
                {active.id === CONSOLIDATED_ID && (
                  <span className="text-[10px] text-brand-400">●</span>
                )}
              </button>
            </>
          )}
        </div>
      )}
    </div>
  );
}
