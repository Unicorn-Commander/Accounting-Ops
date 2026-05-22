"use client";

import { useRouter } from "next/navigation";
import { useState, useTransition } from "react";
import { Building2, ChevronDown, Layers, ShieldCheck } from "lucide-react";
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

  const active =
    entities.find((e) => e.id === activeEntityId) ??
    (activeEntityId === CONSOLIDATED_ID && consolidated
      ? { ...consolidated, slug: CONSOLIDATED_ID, legal_name: null, country_code: "US", functional_currency: "USD", hipaa_protected: false, status: "active", created_at: "" }
      : entities[0]);

  function selectEntity(entityId: string) {
    document.cookie = `aops_entity=${entityId}; Path=/; SameSite=Lax; Max-Age=2592000`;
    setOpen(false);
    startTransition(() => router.refresh());
  }

  return (
    <div className="relative">
      <button
        onClick={() => setOpen(!open)}
        className={cn(
          "flex items-center gap-2 rounded-md border border-default bg-surface transition hover:bg-elevated",
          size === "sm" ? "px-2.5 py-1 text-xs" : "px-3 py-1.5 text-sm"
        )}
      >
        {active.id === CONSOLIDATED_ID ? (
          <Layers
            size={size === "sm" ? 12 : 14}
            className="text-brand-400"
          />
        ) : (
          <Building2 size={size === "sm" ? 12 : 14} className="text-ink-tertiary" />
        )}
        <span className="font-medium">{active.display_name}</span>
        <ChevronDown
          size={size === "sm" ? 11 : 13}
          className={cn(
            "text-ink-tertiary transition-transform",
            open && "rotate-180"
          )}
        />
      </button>

      {open && (
        <div className="absolute right-0 z-40 mt-1.5 w-80 rounded-lg border border-default bg-elevated shadow-card">
          <div className="border-b border-subtle px-3 py-2">
            <p className="text-[10px] font-medium uppercase tracking-wider text-ink-tertiary">
              Entity ({entities.length})
            </p>
          </div>
          <ul className="max-h-80 overflow-y-auto py-1">
            {entities.map((entity) => (
              <li key={entity.id}>
                <button
                  onClick={() => selectEntity(entity.id)}
                  className={cn(
                    "flex w-full items-center gap-3 px-3 py-2 text-left text-sm transition hover:bg-white/[0.03]",
                    entity.id === active.id && "bg-white/[0.02]"
                  )}
                >
                  <Building2
                    size={12}
                    className={
                      entity.id === active.id
                        ? "text-agent-500"
                        : "text-ink-tertiary"
                    }
                  />
                  <div className="flex-1">
                    <div className="flex items-center gap-2">
                      <span className="font-medium">{entity.display_name}</span>
                      {entity.hipaa_protected && (
                        <ShieldCheck
                          size={11}
                          className="text-agent-500"
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
                </button>
              </li>
            ))}
          </ul>
          {consolidated && (
            <>
              <div className="border-t border-subtle" />
              <button
                onClick={() => selectEntity(CONSOLIDATED_ID)}
                className={cn(
                  "flex w-full items-center gap-3 px-3 py-2 text-left text-sm transition hover:bg-white/[0.03]",
                  active.id === CONSOLIDATED_ID && "bg-white/[0.02]"
                )}
              >
                <Layers
                  size={12}
                  className={
                    active.id === CONSOLIDATED_ID
                      ? "text-brand-400"
                      : "text-ink-tertiary"
                  }
                />
                <div className="flex-1">
                  <div className="font-medium">All entities (consolidated)</div>
                  <div className="text-[10px] uppercase tracking-wider text-ink-tertiary">
                    Owner / admin only
                  </div>
                </div>
              </button>
            </>
          )}
        </div>
      )}
    </div>
  );
}
