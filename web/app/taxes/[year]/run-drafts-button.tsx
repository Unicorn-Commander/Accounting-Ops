"use client";

// RunDraftsButton — starts the tax coordinator (1120 + 1099 -> SC1120) for one
// entity + year from the tax page.
//
// Clicking opens a confirm dialog that first loads the server's launch preview
// (GET .../specialist-runs/launch): which model and provider will be used,
// whether it is a third-party API, what data is sent, the cost note, and
// whether a run is already active. Confirming POSTs the same URL; the new
// queued run is handed back to the parent, which shows it in Run history and
// polls it there. The backend is the authority on role, scope and the
// one-active-run rule — this component only renders for accountant+ roles.

import { useCallback, useEffect, useState } from "react";
import { AlertTriangle, Loader2, Play, X } from "lucide-react";
import { cn } from "@/lib/cn";
import type { SpecialistRun } from "@/lib/specialist-runs";
import {
  errorMessage,
  launchUrl,
  type TaxRunPreview
} from "@/lib/tax-run-launch";

export function RunDraftsButton({
  workspaceId,
  entityId,
  entityDisplayName,
  year,
  onLaunched
}: {
  workspaceId: string;
  entityId: string;
  entityDisplayName: string;
  year: number;
  onLaunched: (run: SpecialistRun) => void;
}) {
  const [open, setOpen] = useState(false);
  const [preview, setPreview] = useState<TaxRunPreview | null>(null);
  const [loading, setLoading] = useState(false);
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const close = useCallback(() => {
    if (submitting) return;
    setOpen(false);
  }, [submitting]);

  useEffect(() => {
    if (!open) return;
    let cancelled = false;
    setPreview(null);
    setError(null);
    setLoading(true);
    fetch(launchUrl(workspaceId, entityId, year), { cache: "no-store" })
      .then(async (res) => {
        if (cancelled) return;
        if (!res.ok) {
          setError(await errorMessage(res));
          return;
        }
        setPreview((await res.json()) as TaxRunPreview);
      })
      .catch(() => {
        if (!cancelled) setError("Network error while loading run details.");
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [open, workspaceId, entityId, year]);

  useEffect(() => {
    if (!open) return;
    const handler = (e: KeyboardEvent) => {
      if (e.key === "Escape") {
        e.preventDefault();
        close();
      }
    };
    window.addEventListener("keydown", handler);
    return () => window.removeEventListener("keydown", handler);
  }, [open, close]);

  async function confirm() {
    setSubmitting(true);
    setError(null);
    try {
      const res = await fetch(launchUrl(workspaceId, entityId, year), {
        method: "POST",
        cache: "no-store"
      });
      if (!res.ok) {
        setError(await errorMessage(res));
        return;
      }
      const body = (await res.json()) as { run: SpecialistRun };
      onLaunched(body.run);
      setOpen(false);
    } catch {
      setError("Network error — the run was not started.");
    } finally {
      setSubmitting(false);
    }
  }

  const active = preview?.active_run ?? null;
  const canConfirm = !!preview && preview.enabled && !active && !submitting;

  return (
    <>
      <button
        type="button"
        onClick={() => setOpen(true)}
        className="inline-flex items-center gap-2 rounded-md border border-brand-500/60 bg-brand-500/10 px-4 py-2 text-sm font-semibold text-brand-200 transition-colors hover:bg-brand-500/20"
      >
        <Play size={14} />
        Run drafts
      </button>

      {open && (
        <div
          className="fixed inset-0 z-50 flex items-end justify-center p-4 sm:items-center"
          role="dialog"
          aria-modal="true"
          aria-labelledby="run-drafts-title"
        >
          <button
            type="button"
            aria-label="Close dialog"
            onClick={close}
            className="absolute inset-0 bg-canvas/80 backdrop-blur-sm"
          />
          <div className="relative w-full max-w-lg rounded-lg border border-default bg-elevated p-5 shadow-card">
            <div className="flex items-start justify-between gap-3">
              <div className="min-w-0 flex-1">
                <p className="text-[10px] uppercase tracking-wider text-ink-tertiary">
                  Run tax drafts · {year}
                </p>
                <h2
                  id="run-drafts-title"
                  className="mt-0.5 text-sm font-semibold text-ink-primary"
                >
                  Draft the {year} return package for {entityDisplayName}?
                </h2>
              </div>
              <button
                type="button"
                onClick={close}
                aria-label="Close"
                className="inline-flex h-7 w-7 shrink-0 items-center justify-center rounded-md text-ink-tertiary hover:bg-white/[0.05] hover:text-ink-primary"
              >
                <X size={15} />
              </button>
            </div>

            {loading && (
              <p className="mt-4 inline-flex items-center gap-2 text-xs text-ink-tertiary">
                <Loader2 size={13} className="animate-spin" /> Loading run details…
              </p>
            )}

            {preview && (
              <div className="mt-4 space-y-3 text-xs text-ink-secondary">
                <div
                  className={cn(
                    "rounded-md border px-3 py-2.5",
                    preview.third_party
                      ? "border-pending/40 bg-pending/[0.06]"
                      : "border-default bg-surface"
                  )}
                >
                  <p className="flex items-center gap-1.5 font-semibold text-ink-primary">
                    {preview.third_party && <AlertTriangle size={13} className="text-pending" />}
                    Uses a frontier AI model{preview.third_party ? " over a third-party API" : ""}
                  </p>
                  <p className="mt-1">
                    Model <span className="font-mono text-ink-primary">{preview.model}</span> via{" "}
                    {preview.provider}. {preview.cost_note}
                  </p>
                </div>

                <div>
                  <p className="font-semibold text-ink-primary">
                    Data sent to the model
                  </p>
                  <ul className="mt-1 list-disc space-y-0.5 pl-5">
                    {preview.data_sent.map((d) => (
                      <li key={d}>{d}</li>
                    ))}
                  </ul>
                </div>

                <div>
                  <p className="font-semibold text-ink-primary">What happens</p>
                  <p className="mt-1">
                    Runs {preview.specialists.join(", ")}. Output is DRAFTS for
                    CPA review only: memos and flags land in the review queue and
                    draft files replace this year&apos;s drafts (earlier ones are
                    kept as superseded copies). Nothing is posted to the books,
                    signed or filed. Runs stop after{" "}
                    {Math.round(preview.max_runtime_s / 60)} minutes and can be
                    cancelled from Run history.
                  </p>
                </div>

                {!preview.enabled && preview.disabled_reason && (
                  <p className="rounded-md border border-debit/30 bg-debit/[0.06] px-3 py-2 text-debit">
                    {preview.disabled_reason}
                  </p>
                )}
                {active && (
                  <p className="rounded-md border border-brand-500/30 bg-brand-500/[0.06] px-3 py-2 text-brand-300">
                    A run is already {active.status} for this entity and year.
                    Wait for it to finish or cancel it in Run history.
                  </p>
                )}
              </div>
            )}

            {error && (
              <p className="mt-3 rounded-md border border-debit/30 bg-debit/[0.06] px-3 py-2 text-xs text-debit">
                {error}
              </p>
            )}

            <div className="mt-5 flex justify-end gap-2">
              <button
                type="button"
                onClick={close}
                disabled={submitting}
                className="rounded-md border border-default bg-surface px-3 py-1.5 text-xs text-ink-secondary hover:text-ink-primary disabled:opacity-50"
              >
                Cancel
              </button>
              <button
                type="button"
                onClick={confirm}
                disabled={!canConfirm}
                className="inline-flex items-center gap-1.5 rounded-md bg-brand-500 px-3 py-1.5 text-xs font-semibold text-white hover:bg-brand-600 disabled:cursor-not-allowed disabled:bg-elevated disabled:text-ink-disabled"
              >
                {submitting ? <Loader2 size={12} className="animate-spin" /> : <Play size={12} />}
                {submitting ? "Starting…" : "Run drafts"}
              </button>
            </div>
          </div>
        </div>
      )}
    </>
  );
}
