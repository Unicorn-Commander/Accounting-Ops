// Server-side client for the AI-decision agreement metric endpoint.
//
// Mirrors lib/cfo-brief.ts + lib/cash-position.ts + lib/open-items.ts: a
// no-store fetch to FastAPI using the signed-in user's Keycloak access token.
// The backend (app/api/metrics.py + app/metrics/ai_agreement.py) computes the
// review-vs-redo agreement rate read-only; RBAC + entity scope are enforced
// server-side and the web layer only passes the token through.
//
// Returns null on any failure so the dashboard degrades gracefully — the card
// shows a soft "unavailable" rather than crashing the page (see
// web/components/ai-agreement-card.tsx + web/app/dashboard/page.tsx).

import { auth } from "@/lib/auth";

const INTERNAL_BASE =
  process.env.INTERNAL_API_BASE_URL ??
  process.env.NEXT_PUBLIC_API_BASE_URL ??
  "http://app:8000";

// ---------------------------------------------------------------------------
// Types — mirror the response of
// GET /api/workspaces/{wid}/metrics/ai-agreement (app/api/metrics.py) and the
// pure aggregator in app/metrics/ai_agreement.py.
// ---------------------------------------------------------------------------

// Per-AI-decision breakdown. `approve` / `reject` carry a scored rate;
// `flag_for_review` is a deferral and is never scored (only a count).
export type AiAgreementByDecision = {
  approve?: { total: number; agreed: number; agreement_rate: number | null };
  reject?: { total: number; agreed: number; agreement_rate: number | null };
  flag_for_review?: { deferred: number };
  // Defensive: any other decision value the backend might surface.
  [decision: string]:
    | { total: number; agreed: number; agreement_rate: number | null }
    | { deferred: number }
    | undefined;
};

export type AiAgreementCategoryRow = {
  recommended_category: string | null;
  total: number;
  agreed: number;
  agreement_rate: number | null;
};

// One window's worth of the metric. `agreement_rate` is 0..1 or null (null
// when total_decided === 0 — render as "—" / empty state, NEVER as 0%).
export type AiAgreementWindow = {
  total_decided: number;
  agreed: number;
  agreement_rate: number | null;
  flagged_for_review: number;
  by_recommended_decision: AiAgreementByDecision;
  by_recommended_category: AiAgreementCategoryRow[];
};

export type AiAgreementResponse = {
  workspace_id: string;
  entity_id: string | null;
  metric: "ai_decision_agreement_rate";
  metric_version: number;
  all_time: AiAgreementWindow;
  last_90d: AiAgreementWindow;
  learned_corrections: {
    override_count: number;
    total_match_count: number;
  };
  // Present only when a custom `days` window was requested.
  windowed?: { days: number } & AiAgreementWindow;
};

// ---------------------------------------------------------------------------
// Server fetcher
// ---------------------------------------------------------------------------

export async function fetchAiAgreement(
  workspaceId: string,
  opts: { entityId?: string; days?: number } = {}
): Promise<AiAgreementResponse | null> {
  const session = await auth();
  if (!session?.accessToken) return null;

  const params = new URLSearchParams();
  if (opts.entityId) params.set("entity_id", opts.entityId);
  if (opts.days) params.set("days", String(opts.days));
  const qs = params.toString();

  try {
    const res = await fetch(
      `${INTERNAL_BASE}/api/workspaces/${workspaceId}/metrics/ai-agreement${qs ? "?" + qs : ""}`,
      {
        headers: {
          Authorization: `Bearer ${session.accessToken}`,
          Accept: "application/json"
        },
        cache: "no-store"
      }
    );
    if (!res.ok) return null;
    return (await res.json()) as AiAgreementResponse;
  } catch {
    return null;
  }
}

// ---------------------------------------------------------------------------
// Presentation helper — keep "rate -> %" in one place so the card and any
// future consumer agree. null stays null (the empty-state sentinel); a real
// 0..1 rate becomes a rounded integer percent.
// ---------------------------------------------------------------------------

export function formatAgreementPct(rate: number | null): string | null {
  if (rate === null || Number.isNaN(rate)) return null;
  return `${Math.round(rate * 100)}%`;
}
