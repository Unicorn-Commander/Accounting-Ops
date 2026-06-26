"use client";

// Engagement & filing policy — the per-workspace professional-substitute switch.
//
// This screen drives which actions light up across the tax UI:
//   • review_mode          — is a licensed-pro review REQUIRED, may the user
//                            self-certify, or are autonomous actions off?
//   • has_licensed_pro     — is a CPA/EA/attorney attached to the engagement?
//   • prefer_local_inference — route this workspace's AI to the on-prem model.
//   • submission_channels  — e-sign / e-file (IRS) / state portal / EFTPS.
//   • rail_kill_switch     — emergency: disable ALL channels at once.
//
// Every control applies IMMEDIATELY on change: we optimistically patch local
// state, POST the change, and replace state with the policy the backend
// returns. A small inline "Saved" confirms each write; errors surface beside
// the control. Controls are disabled while a save is in flight. When the
// caller is not an admin, everything renders read-only.

import { useCallback, useEffect, useRef, useState } from "react";
import {
  AlertTriangle,
  Building2,
  Check,
  Loader2,
  PenLine,
  Scale,
  Send,
  ShieldCheck,
} from "lucide-react";
import { Card, ErrorState } from "@/components/primitives";
import { cn } from "@/lib/cn";
import {
  getEngagementPolicy,
  updateEngagementPolicy,
  type EngagementPolicy,
  type ReviewMode,
  type SubmissionChannels,
} from "@/lib/forms";

// ---------------------------------------------------------------------------
// Static copy — review modes + the four submission channels.
// ---------------------------------------------------------------------------

const REVIEW_MODES: {
  value: ReviewMode;
  label: string;
  desc: string;
  icon: React.ElementType;
}[] = [
  {
    value: "pro_required",
    label: "Licensed professional review required",
    desc: "The classic CPA gate. Drafts must be reviewed by a pro before they're final.",
    icon: ShieldCheck,
  },
  {
    value: "self_attest_allowed",
    label: "Self-certification allowed",
    desc: "When no professional is available, the user can review and self-certify their own work — the bypassable gate.",
    icon: PenLine,
  },
  {
    value: "autonomous_off",
    label: "Autonomous actions off",
    desc: "The most conservative setting.",
    icon: AlertTriangle,
  },
];

type ChannelKey = keyof SubmissionChannels;

const CHANNELS: {
  key: ChannelKey;
  label: string;
  helper: string;
  icon: React.ElementType;
}[] = [
  {
    key: "esign",
    label: "E-signature",
    helper:
      "Safe to enable — the client signs their OWN document (no IRS transmission).",
    icon: PenLine,
  },
  {
    key: "efile_irs",
    label: "E-file to the IRS (MeF)",
    helper:
      "Stays gated on your firm's EFIN enrollment even when enabled — the package is generated, the wire is off until enrollment.",
    icon: Send,
  },
  {
    key: "state_portal",
    label: "State e-file portal",
    helper: "Gated on state enrollment.",
    icon: Building2,
  },
  {
    key: "eftps",
    label: "EFTPS tax payments",
    helper: "Gated on enrollment.",
    icon: Building2,
  },
];

// A short-lived "Saved" tag, keyed by control id, that fades on its own.
type SavedMap = Record<string, boolean>;

// ---------------------------------------------------------------------------
// Accessible toggle — button[role=switch]. No switch component exists in the
// codebase, so we build a clean one matching the dark-theme tokens. Tones:
// brand (default) and debit (the emergency kill switch).
// ---------------------------------------------------------------------------

function Toggle({
  checked,
  onChange,
  disabled,
  tone = "brand",
  label,
}: {
  checked: boolean;
  onChange: (next: boolean) => void;
  disabled?: boolean;
  tone?: "brand" | "debit";
  label: string;
}) {
  const onCls = tone === "debit" ? "bg-debit" : "bg-brand-500";
  return (
    <button
      type="button"
      role="switch"
      aria-checked={checked}
      aria-label={label}
      disabled={disabled}
      onClick={() => onChange(!checked)}
      className={cn(
        "relative inline-flex h-6 w-11 shrink-0 items-center rounded-full border transition-colors",
        "focus:outline-none focus-visible:ring-2 focus-visible:ring-brand-500/60 focus-visible:ring-offset-1 focus-visible:ring-offset-canvas",
        checked ? cn(onCls, "border-transparent") : "border-default bg-elevated",
        disabled && "cursor-not-allowed opacity-50"
      )}
    >
      <span
        className={cn(
          "inline-block h-4 w-4 transform rounded-full bg-white shadow-sm transition-transform",
          checked ? "translate-x-6" : "translate-x-1"
        )}
      />
    </button>
  );
}

// A labelled toggle row: icon + title + helper on the left, the switch +
// inline Saved/error/spinner on the right. Used for every boolean control.
function ToggleRow({
  id,
  icon: Icon,
  title,
  helper,
  checked,
  onChange,
  disabled,
  busy,
  saved,
  error,
  tone = "brand",
  overriddenOff,
}: {
  id: string;
  icon: React.ElementType;
  title: string;
  helper: React.ReactNode;
  checked: boolean;
  onChange: (next: boolean) => void;
  disabled?: boolean;
  busy?: boolean;
  saved?: boolean;
  error?: string | null;
  tone?: "brand" | "debit";
  overriddenOff?: boolean;
}) {
  return (
    <div
      className={cn(
        "flex items-start justify-between gap-4 rounded-md border px-3.5 py-3 transition-colors",
        checked && tone === "brand" && "border-brand-500/40 bg-brand-500/[0.05]",
        checked && tone === "debit" && "border-debit/40 bg-debit/[0.06]",
        !checked && "border-subtle bg-surface",
        overriddenOff && "opacity-60"
      )}
    >
      <div className="flex min-w-0 items-start gap-3">
        <span
          className={cn(
            "mt-0.5 flex h-7 w-7 shrink-0 items-center justify-center rounded-md",
            tone === "debit"
              ? "bg-debit/15 text-debit"
              : checked
                ? "bg-brand-500/15 text-brand-300"
                : "bg-elevated text-ink-tertiary"
          )}
        >
          <Icon size={15} aria-hidden="true" />
        </span>
        <div className="min-w-0">
          <p className="text-sm font-medium text-ink-primary">{title}</p>
          <p className="mt-0.5 text-xs leading-relaxed text-ink-secondary">
            {helper}
          </p>
          {overriddenOff ? (
            <p className="mt-1 text-[11px] font-medium text-debit">
              Overridden off by the emergency kill switch.
            </p>
          ) : null}
          {error ? (
            <p className="mt-1 text-[11px] text-debit" title={error}>
              {error}
            </p>
          ) : null}
        </div>
      </div>
      <div className="flex shrink-0 items-center gap-2 pt-0.5">
        <SaveState busy={busy} saved={saved} />
        <Toggle
          checked={checked}
          onChange={onChange}
          disabled={disabled}
          tone={tone}
          label={title}
        />
      </div>
    </div>
  );
}

// The small inline "Saving…" spinner → "Saved" check that every control shows.
function SaveState({ busy, saved }: { busy?: boolean; saved?: boolean }) {
  if (busy) {
    return (
      <span className="inline-flex items-center gap-1 text-[11px] text-ink-tertiary">
        <Loader2 size={11} className="animate-spin" />
        Saving…
      </span>
    );
  }
  if (saved) {
    return (
      <span className="inline-flex items-center gap-1 text-[11px] text-credit">
        <Check size={11} />
        Saved
      </span>
    );
  }
  return null;
}

// ---------------------------------------------------------------------------
// Main client.
// ---------------------------------------------------------------------------

export function EngagementClient({
  workspaceId,
  canAdmin,
}: {
  workspaceId: string;
  canAdmin: boolean;
}) {
  const [policy, setPolicy] = useState<EngagementPolicy | null>(null);
  const [loading, setLoading] = useState(true);
  const [loadError, setLoadError] = useState<string | null>(null);

  // Which control is mid-save (so we disable everything + show its spinner),
  // and which control just succeeded (so we show its transient "Saved").
  const [busyKey, setBusyKey] = useState<string | null>(null);
  const [saved, setSaved] = useState<SavedMap>({});
  const [errors, setErrors] = useState<Record<string, string | null>>({});

  // Track the latest "Saved" timer per key so re-saves reset the fade cleanly.
  const savedTimers = useRef<Record<string, ReturnType<typeof setTimeout>>>({});

  const load = useCallback(async () => {
    setLoading(true);
    setLoadError(null);
    try {
      const p = await getEngagementPolicy(workspaceId);
      setPolicy(p);
    } catch (e) {
      setLoadError(e instanceof Error ? e.message : String(e));
    } finally {
      setLoading(false);
    }
  }, [workspaceId]);

  useEffect(() => {
    void load();
  }, [load]);

  // Clear pending fade timers on unmount.
  useEffect(() => {
    const timers = savedTimers.current;
    return () => {
      Object.values(timers).forEach(clearTimeout);
    };
  }, []);

  function flagSaved(key: string) {
    setSaved((s) => ({ ...s, [key]: true }));
    if (savedTimers.current[key]) clearTimeout(savedTimers.current[key]);
    savedTimers.current[key] = setTimeout(() => {
      setSaved((s) => ({ ...s, [key]: false }));
    }, 2200);
  }

  // The single write path. `key` identifies which control to mark busy/saved/
  // error against; `optimistic` patches local state before the round-trip.
  const apply = useCallback(
    async (
      key: string,
      patch: Parameters<typeof updateEngagementPolicy>[1],
      optimistic: (prev: EngagementPolicy) => EngagementPolicy
    ) => {
      if (!canAdmin || busyKey) return;
      const prev = policy;
      if (!prev) return;

      setBusyKey(key);
      setErrors((e) => ({ ...e, [key]: null }));
      // Optimistic: reflect the change instantly.
      setPolicy(optimistic(prev));

      try {
        const updated = await updateEngagementPolicy(workspaceId, patch);
        // Reload from the authoritative policy the backend returned.
        setPolicy(updated);
        flagSaved(key);
      } catch (e) {
        // Roll back to the pre-change policy and surface the backend detail.
        setPolicy(prev);
        setErrors((er) => ({
          ...er,
          [key]: e instanceof Error ? e.message : "Could not save",
        }));
      } finally {
        setBusyKey(null);
      }
    },
    [canAdmin, busyKey, policy, workspaceId]
  );

  // ----- Loading -----------------------------------------------------------
  if (loading) {
    return (
      <div className="flex items-center gap-2 rounded-lg border border-default bg-surface px-5 py-8 text-sm text-ink-tertiary">
        <Loader2 size={15} className="animate-spin" />
        Loading engagement policy…
      </div>
    );
  }

  // ----- Hard load error ---------------------------------------------------
  if (loadError || !policy) {
    return (
      <ErrorState
        title="Couldn't load the engagement policy"
        description={loadError ?? "No policy was returned for this workspace."}
        recoveryAction={
          <button
            type="button"
            onClick={() => void load()}
            className="inline-flex items-center gap-1.5 rounded-md border border-default bg-elevated px-3 py-1.5 text-xs font-medium text-ink-secondary hover:text-ink-primary"
          >
            <Loader2 size={12} /> Try again
          </button>
        }
      />
    );
  }

  const channels = policy.submission_channels ?? {};
  const killed = policy.rail_kill_switch;
  const disabledAll = !canAdmin || busyKey !== null;

  return (
    <div className="space-y-6">
      {/* Philosophy banner — brand-tinted, matches the W-4P last-mile note. */}
      <div className="flex items-start gap-3 rounded-lg border border-brand-500/30 bg-brand-500/[0.06] px-4 py-3.5">
        <Scale size={16} className="mt-0.5 shrink-0 text-brand-400" />
        <div className="min-w-0 text-xs leading-relaxed text-ink-secondary">
          <p className="font-semibold text-ink-primary">
            These apps are designed to take you all the way — to DO the work of
            a CPA when one isn&apos;t available.
          </p>
          <p className="mt-1">
            This page sets how much human review is required and which
            submission channels are turned on.
          </p>
        </div>
      </div>

      {/* Read-only note for non-admins. */}
      {!canAdmin ? (
        <div className="flex items-start gap-2 rounded-md border border-default bg-elevated/50 px-4 py-3">
          <ShieldCheck size={14} className="mt-0.5 shrink-0 text-ink-tertiary" />
          <p className="text-xs leading-relaxed text-ink-secondary">
            Only workspace owners/admins can change these settings.
          </p>
        </div>
      ) : null}

      {/* ----- Review mode — the headline ----- */}
      <Card>
        <div className="mb-3 flex items-start justify-between gap-3">
          <div>
            <h2 className="text-sm font-semibold text-ink-primary">
              Review mode
            </h2>
            <p className="mt-0.5 text-xs text-ink-tertiary">
              How much human review is required before work products are final.
            </p>
          </div>
          <div className="pt-0.5">
            <SaveState
              busy={busyKey === "review_mode"}
              saved={saved.review_mode}
            />
          </div>
        </div>

        <div
          className="grid grid-cols-1 gap-2.5 md:grid-cols-3"
          role="radiogroup"
          aria-label="Review mode"
        >
          {REVIEW_MODES.map((m) => {
            const active = policy.review_mode === m.value;
            const Icon = m.icon;
            return (
              <button
                key={m.value}
                type="button"
                role="radio"
                aria-checked={active}
                disabled={disabledAll}
                onClick={() =>
                  apply(
                    "review_mode",
                    { review_mode: m.value },
                    (prev) => ({ ...prev, review_mode: m.value })
                  )
                }
                className={cn(
                  "flex flex-col items-start gap-2 rounded-lg border p-3.5 text-left transition",
                  "focus:outline-none focus-visible:ring-2 focus-visible:ring-brand-500/60",
                  active
                    ? "border-brand-500/60 bg-brand-500/[0.08]"
                    : "border-subtle bg-surface hover:bg-white/[0.04]",
                  disabledAll && "cursor-not-allowed opacity-60"
                )}
              >
                <div className="flex w-full items-center justify-between gap-2">
                  <span
                    className={cn(
                      "flex h-7 w-7 items-center justify-center rounded-md",
                      active
                        ? "bg-brand-500/15 text-brand-300"
                        : "bg-elevated text-ink-tertiary"
                    )}
                  >
                    <Icon size={15} aria-hidden="true" />
                  </span>
                  {active ? (
                    <Check size={14} className="text-brand-400" aria-hidden="true" />
                  ) : (
                    <span
                      className="h-3.5 w-3.5 rounded-full border border-default"
                      aria-hidden="true"
                    />
                  )}
                </div>
                <span
                  className={cn(
                    "text-sm font-medium",
                    active ? "text-brand-200" : "text-ink-primary"
                  )}
                >
                  {m.label}
                </span>
                <span className="text-[11px] leading-relaxed text-ink-secondary">
                  {m.desc}
                </span>
              </button>
            );
          })}
        </div>

        {errors.review_mode ? (
          <p className="mt-2 text-[11px] text-debit">{errors.review_mode}</p>
        ) : null}
      </Card>

      {/* ----- Engagement flags ----- */}
      <Card>
        <h2 className="mb-3 text-sm font-semibold text-ink-primary">
          Engagement
        </h2>
        <div className="space-y-2.5">
          <ToggleRow
            id="has_licensed_pro"
            icon={ShieldCheck}
            title="Has licensed professional"
            helper="Whether a licensed CPA/EA/attorney is attached to this engagement."
            checked={policy.has_licensed_pro}
            disabled={disabledAll}
            busy={busyKey === "has_licensed_pro"}
            saved={saved.has_licensed_pro}
            error={errors.has_licensed_pro}
            onChange={(next) =>
              apply(
                "has_licensed_pro",
                { has_licensed_pro: next },
                (prev) => ({ ...prev, has_licensed_pro: next })
              )
            }
          />
          <ToggleRow
            id="prefer_local_inference"
            icon={ShieldCheck}
            title="Prefer local inference"
            helper="Prefer the on-prem model for this workspace's AI (privacy/cost)."
            checked={Boolean(policy.prefer_local_inference)}
            disabled={disabledAll}
            busy={busyKey === "prefer_local_inference"}
            saved={saved.prefer_local_inference}
            error={errors.prefer_local_inference}
            onChange={(next) =>
              apply(
                "prefer_local_inference",
                { prefer_local_inference: next },
                (prev) => ({ ...prev, prefer_local_inference: next })
              )
            }
          />
        </div>
      </Card>

      {/* ----- Submission channels ----- */}
      <Card>
        <div className="mb-3">
          <h2 className="text-sm font-semibold text-ink-primary">
            Submission channels
          </h2>
          <p className="mt-0.5 text-xs text-ink-tertiary">
            Which delivery rails are turned on. Even when enabled, transmission
            stays gated on the underlying enrollment.
          </p>
        </div>
        <div className="space-y-2.5">
          {CHANNELS.map((ch) => {
            const on = Boolean(channels[ch.key]);
            return (
              <ToggleRow
                key={ch.key}
                id={`channel:${ch.key}`}
                icon={ch.icon}
                title={ch.label}
                helper={ch.helper}
                checked={on}
                // When the kill switch is on, channels are forced off: show
                // them off + flagged, and don't let the toggle fight it.
                disabled={disabledAll || killed}
                overriddenOff={killed && on}
                busy={busyKey === `channel:${ch.key}`}
                saved={saved[`channel:${ch.key}`]}
                error={errors[`channel:${ch.key}`]}
                onChange={(next) =>
                  apply(
                    `channel:${ch.key}`,
                    {
                      // Send the FULL merged channels object — the backend
                      // replaces the map wholesale.
                      submission_channels: { ...channels, [ch.key]: next },
                    },
                    (prev) => ({
                      ...prev,
                      submission_channels: {
                        ...prev.submission_channels,
                        [ch.key]: next,
                      },
                    })
                  )
                }
              />
            );
          })}
        </div>
        {killed ? (
          <p className="mt-3 flex items-center gap-1.5 text-[11px] text-debit">
            <AlertTriangle size={12} />
            The emergency kill switch is ON — all channels are disabled
            regardless of the toggles above.
          </p>
        ) : null}
      </Card>

      {/* ----- Emergency kill switch ----- */}
      <Card
        className={cn(
          killed ? "border-debit/50 bg-debit/[0.05]" : "border-debit/25"
        )}
      >
        <ToggleRow
          id="rail_kill_switch"
          icon={AlertTriangle}
          title="Emergency kill switch"
          helper="Immediately disable ALL submission channels regardless of the toggles above."
          checked={killed}
          tone="debit"
          disabled={disabledAll}
          busy={busyKey === "rail_kill_switch"}
          saved={saved.rail_kill_switch}
          error={errors.rail_kill_switch}
          onChange={(next) =>
            apply(
              "rail_kill_switch",
              { rail_kill_switch: next },
              (prev) => ({ ...prev, rail_kill_switch: next })
            )
          }
        />
      </Card>
    </div>
  );
}
