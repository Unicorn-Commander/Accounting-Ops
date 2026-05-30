"use client";

import { ChevronDown, Sparkles } from "lucide-react";
import { useEffect, useRef, useState } from "react";
import { cn } from "@/lib/cn";
import type { ChatPersona } from "./types";

/**
 * Dropdown that picks the agent persona for the current chat session.
 *
 * The chosen persona is bound at session-create time on the backend — the
 * dropdown is read-only for an existing conversation (changing it would
 * require starting a new chat). The wrapper component handles that by
 * showing this picker only when the user is staging a new chat.
 */
export function PersonaPicker({
  personas,
  value,
  onChange,
  disabled = false,
  compact = false
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

  return (
    <div ref={ref} className="relative">
      <button
        type="button"
        onClick={() => !disabled && setOpen((o) => !o)}
        disabled={disabled}
        className={cn(
          "inline-flex items-center gap-1.5 rounded-md border border-default bg-elevated/60",
          "px-2.5 py-1.5 text-xs font-medium text-ink-primary hover:bg-elevated",
          "disabled:cursor-not-allowed disabled:opacity-60",
          compact && "py-1"
        )}
      >
        <Sparkles size={12} className="text-agent-500" />
        <span className="truncate max-w-[12rem]">{active?.display_name ?? "(pick agent)"}</span>
        <ChevronDown size={12} className="text-ink-tertiary" />
      </button>
      {open && (
        <div
          role="listbox"
          aria-label="Agent persona"
          className="absolute left-0 z-30 mt-1 w-[24rem] max-w-[calc(100vw-1rem)] overflow-hidden rounded-md border border-default bg-surface shadow-card"
        >
          <ul className="max-h-[60vh] overflow-y-auto py-1">
            {personas.map((p) => {
              const selected = p.slug === value;
              return (
                <li key={p.slug}>
                  <button
                    type="button"
                    onClick={() => {
                      onChange(p.slug);
                      setOpen(false);
                    }}
                    className={cn(
                      "block w-full px-3 py-2 text-left text-xs hover:bg-white/[0.05]",
                      selected && "bg-brand-500/[0.08]"
                    )}
                  >
                    <div className="flex items-center gap-2">
                      <Sparkles
                        size={11}
                        className={
                          selected ? "text-brand-300" : "text-agent-500/70"
                        }
                      />
                      <p className="font-medium text-ink-primary">{p.display_name}</p>
                    </div>
                    <p className="mt-0.5 pl-5 text-[10px] text-ink-tertiary">{p.one_line}</p>
                  </button>
                </li>
              );
            })}
          </ul>
        </div>
      )}
    </div>
  );
}
