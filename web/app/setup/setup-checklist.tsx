"use client";

// SetupChecklist — persistent left-rail of the Setup Assistant flow.
//
// What it does:
//   * Renders a 6-step vertical checklist (Business profile, Entity, Tax
//     facts, Banking, COA, Review).
//   * Loads + persists state to GET/PATCH /api/workspaces/{wid}/setup-progress
//     via the generic /api/proxy.
//   * Listens for `progress` events from the Setup Assistant chat (the parent
//     SetupClient forwards every assistant tool_call to onAssistantToolCall —
//     we infer step completion from specific tool names — see STEP_MAP below).
//   * Exposes a "Skip and continue" button that marks the workspace as
//     `skipped=true` and redirects to /settings.
//
// Mobile responsiveness:
//   * Below `md` the checklist collapses into a horizontal step indicator
//     (1 line, current step bold) above the chat.
//   * At `md+` it sits as a left rail.

import { useCallback, useEffect, useImperativeHandle, useState, forwardRef } from "react";
import { useRouter } from "next/navigation";
import {
  Building2,
  CheckCircle2,
  Circle,
  Landmark,
  Loader2,
  PauseCircle,
  ReceiptText,
  Sparkles,
  SkipForward,
  ListChecks,
  PiggyBank,
} from "lucide-react";
import { cn } from "@/lib/cn";

export type StepStatus = "not_started" | "current" | "blocked" | "complete";

export type StepKey =
  | "business_profile"
  | "entity"
  | "tax_facts"
  | "banking"
  | "coa"
  | "review";

const STEP_ORDER: StepKey[] = [
  "business_profile",
  "entity",
  "tax_facts",
  "banking",
  "coa",
  "review",
];

const STEP_META: Record<
  StepKey,
  { label: string; one_line: string; icon: typeof Building2 }
> = {
  business_profile: {
    label: "Business profile",
    one_line: "Legal name, address, fiscal year",
    icon: Building2,
  },
  entity: {
    label: "Entity",
    one_line: "Entity kind (C-Corp, LLC, sole prop, etc.)",
    icon: Landmark,
  },
  tax_facts: {
    label: "Tax facts",
    one_line: "EIN, state(s), prior-year filings",
    icon: ReceiptText,
  },
  banking: {
    label: "Banking",
    one_line: "Connect SimpleFin / Mercury / BofA",
    icon: PiggyBank,
  },
  coa: {
    label: "Chart of accounts",
    one_line: "Seeded from template; can override",
    icon: ListChecks,
  },
  review: {
    label: "Review",
    one_line: "Confirm and finish",
    icon: CheckCircle2,
  },
};

export type SetupProgress = {
  version: number;
  skipped: boolean;
  completed_at: string | null;
  steps: Record<StepKey, { status: StepStatus; updated_at: string | null }>;
};

// Map specific Setup Assistant MCP tools to the step they "complete".
// This is intentionally conservative: only tool *results* with a successful
// payload should advance the checklist (the wrapper handles result-presence).
//
// The keys are MCP tool names; the values are the step they advance.
const STEP_MAP: Record<string, StepKey> = {
  create_workspace: "business_profile",
  create_entity_with_coa: "entity",
  // entity_corp_facts.upsert / equivalent — not a registered MCP tool today,
  // so we instead infer from the persona's `flag_for_cpa_review` on tax facts.
  // For now the Setup Assistant's own intake form covers tax_facts; users will
  // update the step manually if they skip to it.
  suggest_connectors: "banking",
  create_coa_account: "coa",
  list_chart_of_accounts: "coa",
  mark_intake_complete: "review",
};

export type SetupChecklistHandle = {
  // Called by the chat wrapper when an assistant tool finishes.
  noteToolEvent: (event: {
    name: string;
    args: Record<string, unknown>;
    result?: unknown;
  }) => void;
};

export const SetupChecklist = forwardRef<
  SetupChecklistHandle,
  {
    workspaceId: string;
    className?: string;
  }
>(function SetupChecklist({ workspaceId, className }, ref) {
  const router = useRouter();
  const [progress, setProgress] = useState<SetupProgress | null>(null);
  const [loading, setLoading] = useState(true);
  const [saveError, setSaveError] = useState<string | null>(null);
  const [skipping, setSkipping] = useState(false);

  // ---- Initial fetch ----
  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        const res = await fetch(
          `/api/proxy/api/workspaces/${workspaceId}/setup-progress`,
          { cache: "no-store" }
        );
        if (!res.ok) throw new Error(`HTTP ${res.status}`);
        const data = (await res.json()) as SetupProgress;
        if (!cancelled) setProgress(data);
      } catch (err) {
        if (!cancelled) {
          setSaveError(err instanceof Error ? err.message : "Failed to load");
        }
      } finally {
        if (!cancelled) setLoading(false);
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [workspaceId]);

  // ---- Persist a partial update ----
  // Note: `steps` is intentionally `Partial<...>`; we only send the keys that
  // change. The backend merges into the persisted document and stamps
  // `updated_at` per step server-side.
  type PatchPayload = {
    skipped?: boolean;
    completed_at?: string | null;
    steps?: Partial<Record<StepKey, { status: StepStatus }>>;
  };
  const patch = useCallback(
    async (delta: PatchPayload) => {
      setSaveError(null);
      try {
        const res = await fetch(
          `/api/proxy/api/workspaces/${workspaceId}/setup-progress`,
          {
            method: "PATCH",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify(delta),
          }
        );
        if (!res.ok) {
          const detail = await res.text().catch(() => "");
          throw new Error(detail || `HTTP ${res.status}`);
        }
        const data = (await res.json()) as SetupProgress;
        setProgress(data);
        return data;
      } catch (err) {
        setSaveError(err instanceof Error ? err.message : "Save failed");
        return null;
      }
    },
    [workspaceId]
  );

  // ---- Mark a step complete (idempotent) ----
  const markStep = useCallback(
    (step: StepKey, status: StepStatus) => {
      if (!progress) return;
      const current = progress.steps[step]?.status;
      if (current === status) return; // already there
      void patch({ steps: { [step]: { status } } });
    },
    [progress, patch]
  );

  // Imperative handle so the chat wrapper can forward tool events without
  // making the checklist a child of the chat.
  useImperativeHandle(
    ref,
    () => ({
      noteToolEvent: (event) => {
        const step = STEP_MAP[event.name];
        if (!step) return;
        // For mark_intake_complete, also stamp completed_at.
        if (event.name === "mark_intake_complete") {
          void patch({
            steps: { [step]: { status: "complete" } },
            completed_at: new Date().toISOString(),
          });
          return;
        }
        markStep(step, "complete");
      },
    }),
    [patch, markStep]
  );

  // ---- Skip-and-continue ----
  const onSkip = useCallback(async () => {
    if (skipping) return;
    setSkipping(true);
    try {
      await patch({ skipped: true });
      router.push("/dashboard");
    } finally {
      setSkipping(false);
    }
  }, [skipping, patch, router]);

  if (loading) {
    return (
      <aside
        className={cn(
          "rounded-xl border border-default bg-surface p-4 text-xs text-ink-tertiary",
          className
        )}
      >
        <span className="inline-flex items-center gap-1.5">
          <Loader2 size={12} className="animate-spin" />
          Loading checklist…
        </span>
      </aside>
    );
  }

  if (!progress) {
    return (
      <aside
        className={cn(
          "rounded-xl border border-debit/40 bg-debit/[0.04] p-4 text-xs text-debit",
          className
        )}
      >
        Checklist unavailable. {saveError ? `(${saveError})` : null}
      </aside>
    );
  }

  // Derive a "current" step heuristically: the first not-yet-complete step.
  // This stays a derived view; persisted state controls completion.
  const derivedCurrentIdx = STEP_ORDER.findIndex(
    (k) => progress.steps[k]?.status !== "complete"
  );
  const allComplete = derivedCurrentIdx === -1;
  const completedCount = STEP_ORDER.filter(
    (k) => progress.steps[k]?.status === "complete"
  ).length;
  // 1-based number of the step the user is on right now (for "Step N of 6").
  const currentStepNumber = allComplete ? STEP_ORDER.length : derivedCurrentIdx + 1;
  const currentStepLabel = allComplete
    ? null
    : STEP_META[STEP_ORDER[derivedCurrentIdx]].label;

  return (
    <aside
      className={cn(
        "flex flex-col gap-3 rounded-xl border border-default bg-surface p-4",
        className
      )}
      aria-label="Setup checklist"
    >
      {/* Header */}
      <div className="flex items-center justify-between gap-2">
        <div className="flex items-center gap-1.5">
          <Sparkles size={13} className="text-agent-500" />
          <h2 className="text-sm font-semibold text-ink-primary">Setup</h2>
        </div>
        <span className="font-mono text-[10px] text-ink-tertiary">
          {completedCount} / {STEP_ORDER.length}
        </span>
      </div>

      {/* Progress bar */}
      <div className="h-1 w-full overflow-hidden rounded-full bg-elevated">
        <div
          className="h-full bg-brand-500 transition-all"
          style={{ width: `${(completedCount / STEP_ORDER.length) * 100}%` }}
        />
      </div>

      {/* "You are here" — makes the live position obvious so a confused user
          always knows which step they're on, not just how many are done. */}
      {!allComplete && currentStepLabel && (
        <div className="flex items-baseline gap-1.5 text-xs">
          <span className="font-semibold text-brand-300">
            Step {currentStepNumber} of {STEP_ORDER.length}
          </span>
          <span className="truncate text-ink-secondary">— {currentStepLabel}</span>
        </div>
      )}

      {/* Compact horizontal indicator (mobile only) */}
      <ol className="flex items-center gap-1.5 overflow-x-auto md:hidden" aria-hidden="false">
        {STEP_ORDER.map((step, i) => {
          const s = progress.steps[step].status;
          const isCurrent = i === derivedCurrentIdx;
          return (
            <li
              key={step}
              className={cn(
                "flex shrink-0 items-center gap-1 rounded-full border px-2 py-0.5 text-[10px]",
                s === "complete"
                  ? "border-credit/40 bg-credit/10 text-credit"
                  : isCurrent
                  ? "border-brand-500/40 bg-brand-500/10 text-brand-300"
                  : "border-subtle bg-elevated text-ink-tertiary"
              )}
            >
              {s === "complete" ? <CheckCircle2 size={9} /> : <Circle size={9} />}
              <span>{i + 1}</span>
            </li>
          );
        })}
      </ol>

      {/* Full vertical list (md+) */}
      <ol className="hidden flex-col gap-1 md:flex">
        {STEP_ORDER.map((step, i) => {
          const meta = STEP_META[step];
          const s = progress.steps[step].status;
          const isCurrent = i === derivedCurrentIdx;
          const Icon = meta.icon;
          return (
            <li
              key={step}
              className={cn(
                "flex items-start gap-2 rounded-md px-2 py-1.5 text-xs transition",
                // The current step gets a left accent + tint so "you are here"
                // is unmistakable, even before any step has completed.
                isCurrent && "border-l-2 border-brand-400 bg-brand-500/[0.08] pl-1.5 text-brand-300",
                !isCurrent && s === "complete" && "text-credit",
                !isCurrent && s === "blocked" && "text-pending",
                !isCurrent && s !== "complete" && s !== "blocked" && "text-ink-secondary"
              )}
              aria-current={isCurrent ? "step" : undefined}
            >
              <span
                className={cn(
                  "mt-0.5 inline-flex h-4 w-4 shrink-0 items-center justify-center"
                )}
                aria-hidden="true"
              >
                {isCurrent ? (
                  <Icon size={14} className="text-brand-300" />
                ) : s === "complete" ? (
                  <CheckCircle2 size={14} />
                ) : s === "blocked" ? (
                  <PauseCircle size={14} />
                ) : (
                  <Circle size={14} className="text-ink-tertiary" />
                )}
              </span>
              <div className="min-w-0 flex-1">
                <p className={cn("truncate", isCurrent ? "font-semibold" : "font-medium")}>
                  {meta.label}
                </p>
                <p className="truncate text-[10px] text-ink-tertiary">
                  {meta.one_line}
                </p>
              </div>
              {isCurrent && (
                <span className="shrink-0 rounded-sm bg-brand-500/20 px-1.5 py-0.5 text-[9px] font-semibold uppercase tracking-wider text-brand-300">
                  You are here
                </span>
              )}
            </li>
          );
        })}
      </ol>

      {/* Completion state */}
      {allComplete && (
        <div className="rounded-md border border-credit/40 bg-credit/10 px-3 py-2 text-xs text-credit">
          All steps complete. The Setup Assistant will route you to the
          dashboard shortly.
        </div>
      )}

      {progress.skipped && (
        <div className="rounded-md border border-pending/40 bg-pending/10 px-3 py-2 text-xs text-pending">
          Setup marked as skipped. You can finish later from /settings.
        </div>
      )}

      {saveError && (
        <p className="text-[10px] text-debit">Couldn&apos;t save: {saveError}</p>
      )}

      {/* Skip-and-continue */}
      {!allComplete && !progress.skipped && (
        <button
          type="button"
          onClick={onSkip}
          disabled={skipping}
          className={cn(
            "mt-1 inline-flex items-center justify-center gap-1.5 rounded-md border border-subtle bg-elevated px-3 py-1.5 text-xs font-medium",
            "text-ink-secondary hover:bg-surface disabled:opacity-50"
          )}
        >
          {skipping ? (
            <Loader2 size={11} className="animate-spin" />
          ) : (
            <SkipForward size={11} />
          )}
          Skip and continue
        </button>
      )}
    </aside>
  );
});
