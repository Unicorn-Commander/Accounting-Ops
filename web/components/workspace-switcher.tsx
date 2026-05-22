"use client";

import { ChevronDown, Plus } from "lucide-react";
import { useState } from "react";
import { cn } from "@/lib/cn";

// Phase 1: hydrate from /api/me + /api/workspaces. Today: placeholder list.
const WORKSPACES = [
  { id: "ws-aaron", name: "Aaron Stransky", role: "owner", entities: 4 },
  { id: "ws-demo-law", name: "Demo Law Firm PLLC", role: "viewer", entities: 2 }
];

export function WorkspaceSwitcher() {
  const [active, setActive] = useState(WORKSPACES[0]);
  const [open, setOpen] = useState(false);

  return (
    <div className="relative">
      <button
        onClick={() => setOpen(!open)}
        className="flex items-center gap-2 rounded-md border border-default bg-surface px-3 py-1.5 text-sm transition hover:bg-elevated"
      >
        <span className="h-2 w-2 rounded-full bg-credit shadow-[0_0_4px_hsl(157_64%_52%_/_0.6)]" />
        <span className="font-medium">{active.name}</span>
        <span className="text-[10px] uppercase tracking-wider text-ink-tertiary">
          {active.role}
        </span>
        <ChevronDown
          size={13}
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
              Switch workspace
            </p>
          </div>
          <ul className="py-1">
            {WORKSPACES.map((ws) => (
              <li key={ws.id}>
                <button
                  onClick={() => {
                    setActive(ws);
                    setOpen(false);
                  }}
                  className={cn(
                    "flex w-full items-center gap-3 px-3 py-2.5 text-left text-sm transition hover:bg-white/[0.03]",
                    ws.id === active.id && "bg-white/[0.02]"
                  )}
                >
                  <span
                    className={cn(
                      "h-1.5 w-1.5 rounded-full",
                      ws.id === active.id ? "bg-credit" : "bg-ink-disabled"
                    )}
                  />
                  <div className="flex-1">
                    <div className="font-medium">{ws.name}</div>
                    <div className="text-[11px] text-ink-tertiary">
                      {ws.entities} entities · {ws.role}
                    </div>
                  </div>
                </button>
              </li>
            ))}
          </ul>
          <div className="border-t border-subtle p-2">
            <button className="flex w-full items-center gap-2 rounded px-2 py-1.5 text-sm text-ink-secondary hover:bg-white/[0.03] hover:text-ink-primary">
              <Plus size={13} />
              Create workspace
            </button>
          </div>
        </div>
      )}
    </div>
  );
}
