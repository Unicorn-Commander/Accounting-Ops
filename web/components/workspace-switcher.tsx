"use client";

import { useRouter } from "next/navigation";
import { useState, useTransition } from "react";
import { ChevronDown, Plus, ShieldCheck } from "lucide-react";
import { cn } from "@/lib/cn";
import type { Workspace } from "@/lib/api";

export function WorkspaceSwitcher({
  workspaces,
  activeWorkspaceId
}: {
  workspaces: Workspace[];
  activeWorkspaceId?: string;
}) {
  const router = useRouter();
  const [open, setOpen] = useState(false);
  const [, startTransition] = useTransition();

  if (workspaces.length === 0) {
    return (
      <div className="rounded-md border border-pending/40 bg-pending/[0.06] px-3 py-1.5 text-xs text-pending">
        No workspaces · contact admin
      </div>
    );
  }

  const active =
    workspaces.find((w) => w.id === activeWorkspaceId) ?? workspaces[0];

  function selectWorkspace(workspaceId: string) {
    document.cookie = `aops_workspace=${workspaceId}; Path=/; SameSite=Lax; Max-Age=2592000`;
    document.cookie = `aops_entity=; Path=/; SameSite=Lax; Max-Age=0`;
    setOpen(false);
    startTransition(() => router.refresh());
  }

  return (
    <div className="relative">
      <button
        onClick={() => setOpen(!open)}
        className="flex items-center gap-2 rounded-md border border-default bg-surface px-3 py-1.5 text-sm transition hover:bg-elevated"
      >
        <span className="h-2 w-2 rounded-full bg-credit shadow-[0_0_4px_hsl(157_64%_52%_/_0.6)]" />
        <span className="font-medium">{active.display_name}</span>
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
        <div className="absolute right-0 z-40 mt-1.5 w-80 rounded-lg border border-default bg-elevated shadow-card">
          <div className="border-b border-subtle px-3 py-2">
            <p className="text-[10px] font-medium uppercase tracking-wider text-ink-tertiary">
              Switch workspace ({workspaces.length})
            </p>
          </div>
          <ul className="max-h-80 overflow-y-auto py-1">
            {workspaces.map((ws) => (
              <li key={ws.id}>
                <button
                  onClick={() => selectWorkspace(ws.id)}
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
                    <div className="flex items-center gap-2">
                      <span className="font-medium">{ws.display_name}</span>
                      {ws.hipaa_protected && (
                        <ShieldCheck
                          size={11}
                          className="text-agent-500"
                          aria-label="HIPAA-protected"
                        />
                      )}
                    </div>
                    <div className="text-[11px] text-ink-tertiary">
                      {ws.entity_count} {ws.entity_count === 1 ? "entity" : "entities"}{" "}
                      · {ws.role} · {ws.isolation_tier}
                    </div>
                  </div>
                </button>
              </li>
            ))}
          </ul>
          <div className="border-t border-subtle p-2">
            <button
              className="flex w-full items-center gap-2 rounded px-2 py-1.5 text-sm text-ink-secondary hover:bg-white/[0.03] hover:text-ink-primary"
              onClick={() =>
                window.alert("Workspace creation lands in Phase 1.")
              }
            >
              <Plus size={13} />
              Create workspace
            </button>
          </div>
        </div>
      )}
    </div>
  );
}
