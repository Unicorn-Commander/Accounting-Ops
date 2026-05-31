"use client";

// Simple modal for capturing an optional "different choice" note on a
// pending decision.  Audit issue: replaces the previous `window.prompt()`,
// which felt like a developer workaround inside a regulated workflow.
//
// Implementation notes:
//   * Uses a portal-free fixed overlay; we don't have a Radix dialog wired
//     into this view yet and the audit prohibits adding new deps.
//   * Escape + backdrop click + Cancel all dismiss without writing.
//   * Submit returns the trimmed note (empty string allowed) to the caller.

import { useEffect, useRef, useState } from "react";
import { X } from "lucide-react";
import { cn } from "@/lib/cn";

export function DecisionNoteDialog({
  open,
  question,
  defaultValue = "",
  onCancel,
  onSubmit
}: {
  open: boolean;
  question: string;
  defaultValue?: string;
  onCancel: () => void;
  onSubmit: (note: string) => void;
}) {
  const [note, setNote] = useState(defaultValue);
  const textareaRef = useRef<HTMLTextAreaElement | null>(null);

  // Reset state every time the dialog opens for a new decision.
  useEffect(() => {
    if (open) {
      setNote(defaultValue);
      // autofocus after the modal mounts
      const id = window.setTimeout(() => textareaRef.current?.focus(), 50);
      return () => window.clearTimeout(id);
    }
  }, [open, defaultValue]);

  // Escape to cancel.
  useEffect(() => {
    if (!open) return;
    const handler = (e: KeyboardEvent) => {
      if (e.key === "Escape") {
        e.preventDefault();
        onCancel();
      }
    };
    window.addEventListener("keydown", handler);
    return () => window.removeEventListener("keydown", handler);
  }, [open, onCancel]);

  // Lock background scroll while the modal is up.
  useEffect(() => {
    if (!open) return;
    const prev = document.body.style.overflow;
    document.body.style.overflow = "hidden";
    return () => {
      document.body.style.overflow = prev;
    };
  }, [open]);

  if (!open) return null;

  return (
    <div
      className="fixed inset-0 z-50 flex items-end justify-center p-4 sm:items-center"
      role="dialog"
      aria-modal="true"
      aria-labelledby="decision-note-title"
    >
      {/* Backdrop */}
      <button
        type="button"
        aria-label="Close dialog"
        onClick={onCancel}
        className="absolute inset-0 bg-canvas/80 backdrop-blur-sm"
      />
      {/* Dialog */}
      <div
        className={cn(
          "relative w-full max-w-md rounded-xl border border-default bg-elevated p-5 shadow-card"
        )}
      >
        <div className="flex items-start justify-between gap-3">
          <div className="min-w-0 flex-1">
            <p className="text-[10px] uppercase tracking-wider text-ink-tertiary">
              Different choice
            </p>
            <h2
              id="decision-note-title"
              className="mt-0.5 text-sm font-semibold text-ink-primary"
            >
              {question}
            </h2>
          </div>
          <button
            type="button"
            onClick={onCancel}
            aria-label="Close"
            className="inline-flex h-7 w-7 shrink-0 items-center justify-center rounded-md text-ink-tertiary hover:bg-white/[0.05] hover:text-ink-primary focus:outline-none focus-visible:ring-2 focus-visible:ring-brand-500 focus-visible:ring-offset-2 focus-visible:ring-offset-canvas"
          >
            <X size={15} />
          </button>
        </div>

        <label className="mt-4 block text-[11px] text-ink-tertiary">
          Describe your decision (optional)
        </label>
        <textarea
          ref={textareaRef}
          value={note}
          onChange={(e) => setNote(e.target.value)}
          rows={4}
          placeholder="e.g. Going with a more conservative deduction stance."
          className="mt-1 w-full resize-none rounded-md border border-default bg-surface px-3 py-2 text-sm text-ink-primary placeholder:text-ink-tertiary focus:border-brand-500 focus:outline-none focus-visible:ring-2 focus-visible:ring-brand-500 focus-visible:ring-offset-2 focus-visible:ring-offset-elevated"
        />

        <div className="mt-4 flex flex-wrap items-center justify-end gap-2">
          <button
            type="button"
            onClick={onCancel}
            className="rounded-md border border-strong bg-white/[0.02] px-3 py-1.5 text-xs font-medium text-ink-secondary transition hover:bg-white/[0.06] hover:text-ink-primary focus:outline-none focus-visible:ring-2 focus-visible:ring-brand-500 focus-visible:ring-offset-2 focus-visible:ring-offset-elevated"
          >
            Cancel
          </button>
          <button
            type="button"
            onClick={() => onSubmit(note.trim())}
            className="rounded-md bg-brand-500 px-3 py-1.5 text-xs font-medium text-white transition hover:bg-brand-600 focus:outline-none focus-visible:ring-2 focus-visible:ring-brand-500 focus-visible:ring-offset-2 focus-visible:ring-offset-elevated"
          >
            Save decision
          </button>
        </div>
      </div>
    </div>
  );
}
