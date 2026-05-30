"use client";

import { MessageSquarePlus, Sparkles } from "lucide-react";
import { cn } from "@/lib/cn";
import type { ChatSessionSummary } from "./types";

/**
 * Left sidebar with a "New chat" button + a list of the user's past
 * sessions sorted by recency. Selecting a session reloads the right pane.
 */
export function ChatSidebar({
  sessions,
  activeId,
  onSelect,
  onNew,
  className
}: {
  sessions: ChatSessionSummary[];
  activeId: string | null;
  onSelect: (id: string) => void;
  onNew: () => void;
  className?: string;
}) {
  return (
    <aside
      className={cn(
        "flex h-full flex-col overflow-hidden rounded-xl border border-default bg-surface",
        className
      )}
      aria-label="Chat sessions"
    >
      <div className="border-b border-subtle p-3">
        <button
          type="button"
          onClick={onNew}
          className="inline-flex w-full items-center justify-center gap-1.5 rounded-md bg-brand-500/15 px-2.5 py-2 text-xs font-medium text-brand-300 hover:bg-brand-500/25"
        >
          <MessageSquarePlus size={13} />
          New chat
        </button>
      </div>
      <div className="flex-1 overflow-y-auto px-2 py-2">
        {sessions.length === 0 ? (
          <p className="px-2 py-3 text-[11px] text-ink-tertiary">
            No conversations yet. Start one with the button above.
          </p>
        ) : (
          <ul className="space-y-1">
            {sessions.map((s) => {
              const isActive = s.id === activeId;
              return (
                <li key={s.id}>
                  <button
                    type="button"
                    onClick={() => onSelect(s.id)}
                    className={cn(
                      "block w-full rounded-md px-2.5 py-2 text-left text-xs transition",
                      isActive
                        ? "bg-brand-500/10 text-ink-primary ring-1 ring-inset ring-brand-500/30"
                        : "text-ink-secondary hover:bg-white/[0.03] hover:text-ink-primary"
                    )}
                  >
                    <p className="line-clamp-2 font-medium leading-snug">
                      {s.title || "Untitled conversation"}
                    </p>
                    <div className="mt-1 flex items-center gap-1.5 text-[10px] text-ink-tertiary">
                      <Sparkles size={9} className="text-agent-500/80" />
                      <span className="truncate">{prettyPersona(s.agent_persona)}</span>
                      <span className="ml-auto whitespace-nowrap">
                        {formatRelative(s.last_activity_at ?? s.started_at)}
                      </span>
                    </div>
                    {s.entity_display_name && (
                      <p className="mt-0.5 truncate text-[10px] text-ink-tertiary">
                        {s.entity_display_name}
                        {s.tax_year ? ` · TY${s.tax_year}` : ""}
                      </p>
                    )}
                  </button>
                </li>
              );
            })}
          </ul>
        )}
      </div>
    </aside>
  );
}

const PERSONA_LABELS: Record<string, string> = {
  "cpa-tax-resolver": "CPA Tax Resolver",
  "form-1120-preparer": "1120 Preparer",
  "sc-state-specialist": "SC State",
  "state-strategy-drafter": "State Strategy",
  "federal-strategy-drafter": "Federal Strategy",
  "1099-specialist": "1099-NEC",
  "officer-compensation-specialist": "Officer Comp",
  "nol-schedule-l-analyst": "NOL / Sched L",
  "audit-defense-specialist": "Audit Defense"
};

function prettyPersona(slug: string | null): string {
  if (!slug) return "Unknown agent";
  return PERSONA_LABELS[slug] ?? slug;
}

function formatRelative(iso: string): string {
  const d = new Date(iso);
  const diffSec = Math.floor((Date.now() - d.getTime()) / 1000);
  if (diffSec < 60) return "just now";
  if (diffSec < 3600) return `${Math.floor(diffSec / 60)}m`;
  if (diffSec < 86400) return `${Math.floor(diffSec / 3600)}h`;
  if (diffSec < 86400 * 7) return `${Math.floor(diffSec / 86400)}d`;
  return d.toLocaleDateString();
}
