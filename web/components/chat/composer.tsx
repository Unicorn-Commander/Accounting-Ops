"use client";

import { ArrowUp, Square } from "lucide-react";
import { useEffect, useRef } from "react";
import { cn } from "@/lib/cn";

/**
 * Multi-line composer with Cmd+Enter (or Ctrl+Enter) to send, Enter for
 * newline, and a Stop button while streaming.
 *
 * The textarea is auto-grow capped at 12 rows.
 */
export function Composer({
  value,
  onChange,
  onSubmit,
  onCancel,
  streaming,
  disabled,
  placeholder,
  footnote = "DRAFT memos and DRAFT recommendations are allowed; final approval still requires an explicit click in the Approvals queue."
}: {
  value: string;
  onChange: (v: string) => void;
  onSubmit: () => void;
  onCancel: () => void;
  streaming: boolean;
  disabled?: boolean;
  placeholder?: string;
  /** Disclaimer under the composer. Defaults to the draft/approval note;
   *  pass a surface-specific string, or "" to hide it (e.g. the read-only Guide). */
  footnote?: string;
}) {
  const ref = useRef<HTMLTextAreaElement>(null);

  // Auto-grow.
  useEffect(() => {
    const el = ref.current;
    if (!el) return;
    el.style.height = "auto";
    const maxPx = Math.round(12 * 22);
    el.style.height = Math.min(el.scrollHeight, maxPx) + "px";
  }, [value]);

  function onKeyDown(e: React.KeyboardEvent<HTMLTextAreaElement>) {
    // Cmd/Ctrl + Enter sends.
    if ((e.metaKey || e.ctrlKey) && e.key === "Enter") {
      e.preventDefault();
      if (!disabled && value.trim() && !streaming) onSubmit();
    }
  }

  return (
    <div className="border-t border-subtle p-3 md:p-4">
      <div className="relative">
        <textarea
          ref={ref}
          rows={2}
          value={value}
          onChange={(e) => onChange(e.target.value)}
          onKeyDown={onKeyDown}
          disabled={disabled}
          placeholder={
            placeholder ??
            "Ask the agent anything — Cmd+Enter to send"
          }
          className={cn(
            "w-full resize-none rounded-lg border border-default bg-canvas px-4 py-3 pr-32 text-sm",
            "placeholder:text-ink-disabled focus:border-brand-500 focus:outline-none",
            "disabled:opacity-50 disabled:cursor-not-allowed"
          )}
        />
        <div className="absolute right-2 top-2 flex items-center gap-1.5">
          <span className="hidden text-[10px] text-ink-tertiary md:inline">
            {streaming ? "" : "Cmd+Enter"}
          </span>
          {streaming ? (
            <button
              type="button"
              onClick={onCancel}
              aria-label="Stop generating"
              className="inline-flex h-7 items-center gap-1 rounded-md bg-pending/20 px-2.5 text-[11px] font-medium text-pending hover:bg-pending/30"
            >
              <Square size={12} fill="currentColor" /> Stop
            </button>
          ) : (
            <button
              type="button"
              onClick={() => onSubmit()}
              disabled={disabled || !value.trim()}
              aria-label="Send message"
              className={cn(
                "inline-flex h-7 items-center gap-1 rounded-md bg-brand-500 px-2.5 text-[11px] font-medium text-white hover:bg-brand-600",
                "disabled:cursor-not-allowed disabled:opacity-40"
              )}
            >
              <ArrowUp size={14} />
            </button>
          )}
        </div>
      </div>
      {footnote ? (
        <p className="mt-2 text-[10px] text-ink-tertiary">{footnote}</p>
      ) : null}
    </div>
  );
}
