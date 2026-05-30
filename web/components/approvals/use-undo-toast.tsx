"use client";

/**
 * useUndoToast
 *
 * Tiny single-toast hook for the approvals UX. The decision has already been
 * written (the API path doesn't actually support undo — financial writes are
 * append-only and audited). Instead this gives the reviewer a 30-second
 * affordance to call ``onUndo`` themselves (which they can wire to ANOTHER
 * write: post a re-categorize approval, etc.).
 *
 * The toast is intentionally minimal: one at a time, fade out after 30s.
 */

import { useState, useRef, useCallback, useEffect } from "react";
import { Check, Undo2 } from "lucide-react";
import { cn } from "@/lib/cn";

export type ToastKind = "approve" | "reject" | "skip";

type ToastState = {
  message: string;
  kind: ToastKind;
  onUndo?: () => void;
  /** server-issued timestamp (ms epoch) used by Aaron's 30-sec undo window */
  issuedAt: number;
};

export function useUndoToast() {
  const [toast, setToast] = useState<ToastState | null>(null);
  const timeoutRef = useRef<ReturnType<typeof setTimeout> | null>(null);

  const dismiss = useCallback(() => {
    if (timeoutRef.current) {
      clearTimeout(timeoutRef.current);
      timeoutRef.current = null;
    }
    setToast(null);
  }, []);

  const showToast = useCallback(
    (message: string, kind: ToastKind, onUndo?: () => void) => {
      if (timeoutRef.current) clearTimeout(timeoutRef.current);
      setToast({ message, kind, onUndo, issuedAt: Date.now() });
      timeoutRef.current = setTimeout(() => setToast(null), 30_000);
    },
    []
  );

  useEffect(() => {
    return () => {
      if (timeoutRef.current) clearTimeout(timeoutRef.current);
    };
  }, []);

  const ToastView = toast ? <ToastBanner toast={toast} onDismiss={dismiss} /> : null;

  return { showToast, dismiss, ToastView };
}

function ToastBanner({
  toast,
  onDismiss
}: {
  toast: ToastState;
  onDismiss: () => void;
}) {
  const tone =
    toast.kind === "approve"
      ? "border-credit/40 bg-credit/10 text-credit"
      : toast.kind === "reject"
        ? "border-debit/40 bg-debit/10 text-debit"
        : "border-pending/40 bg-pending/10 text-pending";

  return (
    <div
      role="status"
      aria-live="polite"
      className="fixed bottom-4 left-1/2 z-50 -translate-x-1/2"
    >
      <div
        className={cn(
          "flex items-center gap-3 rounded-full border bg-surface px-4 py-2 text-sm shadow-xl backdrop-blur",
          tone
        )}
      >
        <Check size={14} />
        <span className="font-medium">{toast.message}</span>
        {toast.onUndo && (
          <button
            type="button"
            onClick={() => {
              toast.onUndo?.();
              onDismiss();
            }}
            className="inline-flex items-center gap-1 rounded-full border border-current/40 bg-surface/40 px-2.5 py-0.5 text-xs font-medium hover:bg-surface/80"
          >
            <Undo2 size={11} />
            Undo
          </button>
        )}
        <button
          type="button"
          onClick={onDismiss}
          aria-label="Dismiss"
          className="text-xs text-ink-tertiary hover:text-ink-secondary"
        >
          ✕
        </button>
      </div>
    </div>
  );
}
