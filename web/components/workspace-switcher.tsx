"use client";

import { useRouter } from "next/navigation";
import { useState, useTransition } from "react";
import { Building2, ChevronDown, Plus, ShieldCheck } from "lucide-react";
import { cn } from "@/lib/cn";
import type { Workspace } from "@/lib/api";
import { queueGlobalToast } from "@/components/global-toast";

// Role chip color map — owner stands out, viewer is faded.
const ROLE_CHIP: Record<string, string> = {
  owner: "bg-brand-500/15 text-brand-300",
  admin: "bg-agent-500/15 text-agent-500",
  accountant: "bg-pending/15 text-pending",
  viewer: "bg-white/5 text-ink-tertiary",
};

const HIERARCHY_TOOLTIP =
  "A workspace contains one or more business entities; switching workspace changes which set of entities you can pick from.";

export function WorkspaceSwitcher({
  workspaces,
  activeWorkspaceId,
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
    if (workspaceId === active.id) {
      setOpen(false);
      return;
    }
    const target = workspaces.find((w) => w.id === workspaceId);
    document.cookie = `aops_workspace=${workspaceId}; Path=/; SameSite=Lax; Max-Age=2592000`;
    document.cookie = `aops_entity=; Path=/; SameSite=Lax; Max-Age=0`;
    // Queue the workspace-switch toast.  Per audit issue H6, we explicitly
    // note that the entity scope reset so the user knows why the active
    // entity changed underneath them.
    if (target) {
      queueGlobalToast(
        `Switched to ${target.display_name}. Entity reset to default.`,
        "brand"
      );
    }
    setOpen(false);
    startTransition(() => router.refresh());
  }

  return (
    <div className="relative">
      <button
        onClick={() => setOpen(!open)}
        title={HIERARCHY_TOOLTIP}
        aria-haspopup="listbox"
        aria-expanded={open}
        className="flex items-center gap-1.5 rounded-md border border-default bg-surface px-2.5 py-1.5 text-sm transition hover:bg-elevated focus:outline-none focus-visible:ring-2 focus-visible:ring-brand-500 focus-visible:ring-offset-2 focus-visible:ring-offset-canvas"
      >
        <Building2 size={13} className="text-ink-tertiary shrink-0" />
        <span className="hidden md:inline text-[11px] uppercase tracking-wider text-ink-tertiary">
          Workspace:
        </span>
        <span className="font-medium truncate max-w-[140px]">{active.display_name}</span>
        <span
          className={cn(
            "hidden sm:inline rounded-full px-1.5 py-0.5 text-[10px] font-medium uppercase tracking-wider",
            ROLE_CHIP[active.role] ?? ROLE_CHIP.viewer
          )}
        >
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
          <div className="border-b border-subtle px-3 py-2.5">
            <p className="text-[10px] font-medium uppercase tracking-wider text-ink-tertiary">
              Switch workspace ({workspaces.length} available)
            </p>
            <p className="mt-1 text-[11px] leading-snug text-ink-tertiary">
              A workspace contains one or more business entities.
            </p>
          </div>
          <ul className="max-h-80 overflow-y-auto py-1" role="listbox">
            {workspaces.map((ws) => (
              <li key={ws.id} role="option" aria-selected={ws.id === active.id}>
                <button
                  onClick={() => selectWorkspace(ws.id)}
                  className={cn(
                    "flex w-full items-center gap-3 px-3 py-2.5 text-left text-sm transition hover:bg-white/[0.03] focus:outline-none focus-visible:bg-white/[0.04]",
                    ws.id === active.id && "bg-white/[0.02]"
                  )}
                >
                  <Building2
                    size={14}
                    className={
                      ws.id === active.id
                        ? "text-brand-300"
                        : "text-ink-tertiary"
                    }
                  />
                  <div className="flex-1 min-w-0">
                    <div className="flex items-center gap-2">
                      <span className="font-medium truncate">{ws.display_name}</span>
                      <span
                        className={cn(
                          "shrink-0 rounded-full px-1.5 py-0.5 text-[9px] font-medium uppercase tracking-wider",
                          ROLE_CHIP[ws.role] ?? ROLE_CHIP.viewer
                        )}
                      >
                        {ws.role}
                      </span>
                      {ws.hipaa_protected && (
                        <ShieldCheck
                          size={11}
                          className="text-agent-500 shrink-0"
                          aria-label="HIPAA-protected"
                        />
                      )}
                    </div>
                    <div className="mt-0.5 text-[11px] text-ink-tertiary">
                      {ws.entity_count} {ws.entity_count === 1 ? "entity" : "entities"}{" "}
                      · {ws.isolation_tier}
                    </div>
                  </div>
                </button>
              </li>
            ))}
          </ul>
          <div className="border-t border-subtle p-2">
            {/* Audit quick-win #4: replace window.alert() with a disabled
                control + tooltip.  We surface the rough timeline instead of
                a popup. */}
            <button
              type="button"
              disabled
              title="Workspace creation lands in Phase 1."
              aria-disabled="true"
              className="flex w-full cursor-not-allowed items-center gap-2 rounded px-2 py-1.5 text-sm text-ink-tertiary opacity-60"
            >
              <Plus size={13} />
              <span className="flex-1 text-left">Create workspace</span>
              <span className="rounded-full bg-white/[0.04] px-1.5 py-0.5 text-[9px] font-medium uppercase tracking-wider text-ink-tertiary">
                Phase 1
              </span>
            </button>
          </div>
        </div>
      )}
    </div>
  );
}
