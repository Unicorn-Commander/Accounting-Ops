"use client";

// GlobalToast — a one-time toast that survives a router.refresh() / SSR round
// trip.  Mounted once at the top of AppShell.
//
// Producers (e.g. WorkspaceSwitcher) call queueGlobalToast(msg) which writes
// to sessionStorage; this component reads + clears that key on mount and
// shows a 5-second toast with an aria-live announce.
//
// Self-imposed constraints (per audit constraints):
//   * No new deps — pure React + Tailwind.
//   * Match dark mode + brand-violet aesthetic.
//   * Auto-dismiss after 5 seconds; dismiss button always available.

import { useEffect, useState } from "react";
import { CheckCircle2, X } from "lucide-react";
import { cn } from "@/lib/cn";

const STORAGE_KEY = "aops_toast_pending";

type ToastTone = "ink" | "credit" | "brand" | "debit" | "pending";

type ToastPayload = {
  message: string;
  tone?: ToastTone;
};

export function queueGlobalToast(message: string, tone: ToastTone = "credit") {
  if (typeof window === "undefined") return;
  try {
    window.sessionStorage.setItem(
      STORAGE_KEY,
      JSON.stringify({ message, tone } satisfies ToastPayload)
    );
    // Notify any GlobalToast mounted in the current SPA frame (no full
    // reload) — without this the toast only appears after a hard refresh.
    window.dispatchEvent(new Event("aops:toast"));
  } catch {
    // sessionStorage may be unavailable (private mode, etc.); silently drop.
  }
}

export function GlobalToast() {
  const [toast, setToast] = useState<ToastPayload | null>(null);

  useEffect(() => {
    const drain = () => {
      try {
        const raw = window.sessionStorage.getItem(STORAGE_KEY);
        if (!raw) return;
        window.sessionStorage.removeItem(STORAGE_KEY);
        const parsed = JSON.parse(raw) as ToastPayload;
        if (parsed && typeof parsed.message === "string") {
          setToast(parsed);
        }
      } catch {
        // ignore malformed payloads
      }
    };

    drain();
    window.addEventListener("aops:toast", drain);
    return () => window.removeEventListener("aops:toast", drain);
  }, []);

  useEffect(() => {
    if (!toast) return;
    const t = window.setTimeout(() => setToast(null), 5000);
    return () => window.clearTimeout(t);
  }, [toast]);

  if (!toast) return null;

  const tone = toast.tone ?? "credit";
  const toneCls: Record<ToastTone, string> = {
    ink: "border-default bg-elevated text-ink-primary",
    credit: "border-credit/40 bg-credit/[0.10] text-credit",
    brand: "border-brand-500/40 bg-brand-500/[0.10] text-brand-300",
    debit: "border-debit/40 bg-debit/[0.10] text-debit",
    pending: "border-pending/40 bg-pending/[0.10] text-pending"
  };

  return (
    <div
      role="status"
      aria-live="polite"
      className="pointer-events-none fixed bottom-4 left-1/2 z-50 -translate-x-1/2 px-4"
    >
      <div
        className={cn(
          "pointer-events-auto flex items-center gap-3 rounded-full border px-4 py-2 text-sm shadow-card backdrop-blur",
          toneCls[tone]
        )}
      >
        <CheckCircle2 size={14} className="shrink-0" />
        <span className="font-medium">{toast.message}</span>
        <button
          type="button"
          aria-label="Dismiss"
          onClick={() => setToast(null)}
          className="-mr-1 inline-flex h-6 w-6 items-center justify-center rounded-full text-current/60 hover:bg-white/[0.06] hover:text-current focus:outline-none focus-visible:ring-2 focus-visible:ring-brand-500"
        >
          <X size={12} />
        </button>
      </div>
    </div>
  );
}
