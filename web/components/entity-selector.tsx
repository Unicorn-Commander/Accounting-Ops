"use client";

import { Building2, ChevronDown } from "lucide-react";
import { useState } from "react";
import { cn } from "@/lib/cn";

// Phase 1: hydrate from the active workspace's entity list.
const ENTITIES = [
  { id: "ent-mu", name: "Magic Unicorn Inc", kind: "C-Corp", default: true },
  { id: "ent-aaron", name: "Aaron Stransky (personal)", kind: "Personal", default: false },
  { id: "ent-majiks", name: "Majiks JV", kind: "JV", default: false },
  { id: "ent-gfl", name: "Brightwater Labs", kind: "JV", default: false }
];

const CONSOLIDATED = {
  id: "consolidated",
  name: "All entities (consolidated)",
  kind: "Workspace"
};

export function EntitySelector({
  showConsolidated = true,
  size = "md"
}: {
  showConsolidated?: boolean;
  size?: "sm" | "md";
}) {
  const [active, setActive] = useState<typeof ENTITIES[number] | typeof CONSOLIDATED>(
    ENTITIES.find((e) => e.default) ?? ENTITIES[0]
  );
  const [open, setOpen] = useState(false);

  return (
    <div className="relative">
      <button
        onClick={() => setOpen(!open)}
        className={cn(
          "flex items-center gap-2 rounded-md border border-default bg-surface transition hover:bg-elevated",
          size === "sm" ? "px-2.5 py-1 text-xs" : "px-3 py-1.5 text-sm"
        )}
      >
        <Building2 size={size === "sm" ? 12 : 14} className="text-ink-tertiary" />
        <span className="font-medium">{active.name}</span>
        <ChevronDown
          size={size === "sm" ? 11 : 13}
          className={cn(
            "text-ink-tertiary transition-transform",
            open && "rotate-180"
          )}
        />
      </button>

      {open && (
        <div className="absolute right-0 z-40 mt-1.5 w-72 rounded-lg border border-default bg-elevated shadow-card">
          <div className="border-b border-subtle px-3 py-2">
            <p className="text-[10px] font-medium uppercase tracking-wider text-ink-tertiary">
              Entity
            </p>
          </div>
          <ul className="py-1">
            {ENTITIES.map((entity) => (
              <li key={entity.id}>
                <button
                  onClick={() => {
                    setActive(entity);
                    setOpen(false);
                  }}
                  className={cn(
                    "flex w-full items-center gap-3 px-3 py-2 text-left text-sm transition hover:bg-white/[0.03]",
                    entity.id === active.id && "bg-white/[0.02]"
                  )}
                >
                  <Building2
                    size={12}
                    className={
                      entity.id === active.id ? "text-agent-500" : "text-ink-tertiary"
                    }
                  />
                  <div className="flex-1">
                    <div className="font-medium">{entity.name}</div>
                    <div className="text-[10px] uppercase tracking-wider text-ink-tertiary">
                      {entity.kind}
                    </div>
                  </div>
                </button>
              </li>
            ))}
          </ul>
          {showConsolidated && (
            <>
              <div className="border-t border-subtle" />
              <button
                onClick={() => {
                  setActive(CONSOLIDATED);
                  setOpen(false);
                }}
                className={cn(
                  "flex w-full items-center gap-3 px-3 py-2 text-left text-sm transition hover:bg-white/[0.03]",
                  active.id === CONSOLIDATED.id && "bg-white/[0.02]"
                )}
              >
                <Building2
                  size={12}
                  className={
                    active.id === CONSOLIDATED.id
                      ? "text-brand-400"
                      : "text-ink-tertiary"
                  }
                />
                <div className="flex-1">
                  <div className="font-medium">{CONSOLIDATED.name}</div>
                  <div className="text-[10px] uppercase tracking-wider text-ink-tertiary">
                    Owner/admin only
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
