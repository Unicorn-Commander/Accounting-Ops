"use client";

// DecisionAuditTrail — user-facing audit history for a single decision.
//
// Reads from GET /api/workspaces/{wid}/open-items/decision-history?key=...
// and renders an inline timeline of every choice the user has recorded for
// the given decision key (Accept / Other / Changed mind), with:
//
//   * Who decided (user uc_uid)
//   * When
//   * What was decided (Accept vs Other; if Other, the note)
//   * Whether reversible (per the reversibility classification — for now
//     decisions older than 30 days are treated as permanent)
//   * Optional "Change…" affordance handled by the parent (passes onChange)
//
// AI-internal IDs (full uc_uid hash, queue_item_id) are hidden by default
// and collapsed into a "Technical details" disclosure.
//
// The trail is READ-ONLY — the user cannot edit history. They can record a
// NEW decision via the parent surface (which appends a new row to the same
// audit table); the trail then reflects the change on next load.

import { useEffect, useState } from "react";
import { AlertCircle, CheckCircle2, ChevronDown, ChevronRight, Clock, User } from "lucide-react";
import { cn } from "@/lib/cn";

export type DecisionHistoryEntry = {
  id: string;
  verdict: string; // "accept" | "other"
  decided_by: string | null;
  decided_at: string;
  notes: string;
};

export type DecisionHistoryResponse = {
  key: string;
  history: DecisionHistoryEntry[];
  total: number;
};

// "Reversible" classification — decisions within this window can be undone
// by recording a new verdict. Matches the open-items behaviour (latest
// INSERT wins).
const REVERSIBLE_WINDOW_HOURS = 24 * 30; // 30 days

function relativeTime(iso: string): string {
  const t = new Date(iso).getTime();
  const now = Date.now();
  const delta = Math.max(0, Math.floor((now - t) / 1000));
  if (delta < 60) return `${delta}s ago`;
  if (delta < 3600) return `${Math.floor(delta / 60)}m ago`;
  if (delta < 86400) return `${Math.floor(delta / 3600)}h ago`;
  const days = Math.floor(delta / 86400);
  if (days < 30) return `${days}d ago`;
  return new Date(iso).toLocaleDateString();
}

function friendlyUser(uc_uid: string | null): string {
  if (!uc_uid) return "—";
  // Show the short prefix — full uc_uid lives behind the technical-details
  // disclosure so the human-facing line isn't dominated by a hash.
  if (uc_uid.length <= 32) return uc_uid;
  return uc_uid.slice(0, 24) + "…";
}

function reversibleUntil(decided_at: string): {
  reversible: boolean;
  hoursLeft: number;
  reversibleUntilStr: string | null;
} {
  const t = new Date(decided_at).getTime();
  const elapsedH = (Date.now() - t) / 3600_000;
  const hoursLeft = Math.max(0, REVERSIBLE_WINDOW_HOURS - elapsedH);
  const reversible = hoursLeft > 0;
  if (!reversible) {
    return { reversible: false, hoursLeft: 0, reversibleUntilStr: null };
  }
  const until = new Date(t + REVERSIBLE_WINDOW_HOURS * 3600_000);
  return {
    reversible: true,
    hoursLeft,
    reversibleUntilStr: until.toLocaleDateString()
  };
}

export function DecisionAuditTrail({
  workspaceId,
  decisionKey,
  onChange,
  variant = "inline"
}: {
  workspaceId: string;
  decisionKey: string;
  onChange?: () => void;
  variant?: "inline" | "card";
}) {
  const [data, setData] = useState<DecisionHistoryResponse | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [showTechnical, setShowTechnical] = useState(false);

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    setError(null);
    (async () => {
      try {
        const res = await fetch(
          `/api/workspaces/${workspaceId}/open-items/decision-history?key=${encodeURIComponent(
            decisionKey
          )}`,
          { cache: "no-store" }
        );
        if (!res.ok) {
          if (!cancelled) {
            setError(`History unavailable (HTTP ${res.status})`);
            setLoading(false);
          }
          return;
        }
        const json = (await res.json()) as DecisionHistoryResponse;
        if (!cancelled) {
          setData(json);
          setLoading(false);
        }
      } catch {
        if (!cancelled) {
          setError("Network error loading decision history");
          setLoading(false);
        }
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [workspaceId, decisionKey]);

  if (loading) {
    return (
      <div className="px-4 py-3 text-[11px] text-ink-tertiary">
        Loading decision history…
      </div>
    );
  }
  if (error) {
    return (
      <div className="px-4 py-3 text-[11px] text-debit">
        <AlertCircle size={11} className="mr-1 inline" />
        {error}
      </div>
    );
  }
  const entries = data?.history ?? [];
  if (entries.length === 0) {
    return (
      <div className="px-4 py-3 text-[11px] text-ink-tertiary">
        No decision history recorded yet.
      </div>
    );
  }

  // Most-recent first for display
  const ordered = [...entries].reverse();
  const latest = ordered[0];
  const latestReversible = reversibleUntil(latest.decided_at);

  const container =
    variant === "card"
      ? "rounded-lg border border-subtle bg-canvas/40 p-3"
      : "border-t border-subtle/60 bg-white/[0.01] px-4 py-3";

  return (
    <div className={container}>
      <div className="mb-2 flex items-center justify-between">
        <p className="text-[10px] font-medium uppercase tracking-wider text-ink-tertiary">
          Decision history ({entries.length})
        </p>
        {latestReversible.reversible && latestReversible.reversibleUntilStr ? (
          <span className="inline-flex items-center gap-1 rounded-md bg-credit/10 px-1.5 py-0.5 text-[10px] font-medium text-credit">
            <Clock size={10} />
            Can undo until {latestReversible.reversibleUntilStr}
          </span>
        ) : (
          <span className="inline-flex items-center gap-1 rounded-md bg-white/[0.05] px-1.5 py-0.5 text-[10px] font-medium text-ink-tertiary">
            Permanent (over 30 days old)
          </span>
        )}
      </div>

      <ol className="space-y-2">
        {ordered.map((e, idx) => {
          const isLatest = idx === 0;
          const isAccept = e.verdict.toLowerCase() === "accept";
          return (
            <li
              key={e.id}
              className={cn(
                "rounded-md border px-3 py-2 text-[11px]",
                isLatest
                  ? isAccept
                    ? "border-credit/40 bg-credit/[0.05]"
                    : "border-brand-500/30 bg-brand-500/[0.05]"
                  : "border-subtle bg-canvas/30 opacity-70"
              )}
            >
              <div className="flex flex-wrap items-center gap-2">
                {isAccept ? (
                  <span className="inline-flex items-center gap-1 rounded-full bg-credit/15 px-1.5 py-0.5 text-[10px] font-medium text-credit">
                    <CheckCircle2 size={10} /> Accepted
                  </span>
                ) : (
                  <span className="inline-flex items-center gap-1 rounded-full bg-brand-500/15 px-1.5 py-0.5 text-[10px] font-medium text-brand-300">
                    Other choice
                  </span>
                )}
                {isLatest ? (
                  <span className="text-[10px] font-medium uppercase tracking-wider text-ink-tertiary">
                    Current
                  </span>
                ) : (
                  <span className="text-[10px] text-ink-tertiary">
                    Previous
                  </span>
                )}
              </div>
              <p className="mt-1 flex flex-wrap items-center gap-1.5 text-ink-secondary">
                <User size={10} className="text-ink-tertiary" />
                <span>Decided by you ({friendlyUser(e.decided_by)})</span>
                <span className="text-ink-tertiary">·</span>
                <span>{relativeTime(e.decided_at)}</span>
                <span className="text-ink-tertiary">
                  · {new Date(e.decided_at).toLocaleString()}
                </span>
              </p>
              {e.notes ? (
                <p className="mt-1 text-ink-secondary">
                  <span className="text-ink-tertiary">Note:</span>{" "}
                  {e.notes}
                </p>
              ) : null}

              {/* Affordance: the parent owns the "record a new decision"
                  surface (Accept / Different choice buttons). The trail's
                  job is to communicate that pressing those buttons appends
                  a row here. */}
              {isLatest && latestReversible.reversible && onChange ? (
                <button
                  type="button"
                  onClick={onChange}
                  className="mt-2 inline-flex items-center gap-1 rounded-md border border-strong bg-canvas/40 px-2.5 py-1 text-[10px] font-medium text-ink-secondary transition hover:bg-canvas/60 hover:text-ink-primary"
                >
                  Change decision…
                </button>
              ) : null}
            </li>
          );
        })}
      </ol>

      <button
        type="button"
        onClick={() => setShowTechnical((s) => !s)}
        className="mt-2 inline-flex items-center gap-1 text-[10px] font-medium uppercase tracking-wider text-ink-tertiary hover:text-ink-secondary"
      >
        {showTechnical ? (
          <ChevronDown size={10} />
        ) : (
          <ChevronRight size={10} />
        )}
        Technical details
      </button>
      {showTechnical ? (
        <pre className="mt-1 max-h-40 overflow-auto rounded-md bg-canvas/60 p-2 text-[10px] leading-relaxed text-ink-secondary">
          {JSON.stringify(
            {
              decision_key: data?.key,
              total_records: data?.total,
              audit_table: "tax_review_queue (form_reference='DECISION_RESOLVED:*')",
              entries: entries.map((e) => ({
                id: e.id,
                verdict: e.verdict,
                decided_by: e.decided_by,
                decided_at: e.decided_at
              }))
            },
            null,
            2
          )}
        </pre>
      ) : null}

      <p className="mt-2 text-[10px] text-ink-tertiary">
        Append-only. Recording a new decision adds a row; nothing is ever
        deleted.
      </p>
    </div>
  );
}
