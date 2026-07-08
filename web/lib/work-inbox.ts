// Server-side client for the cross-client CPA "work inbox" (v1).
//
// Mirrors lib/cfo-brief.ts + lib/api.ts: a no-store fetch to FastAPI using the
// signed-in user's Keycloak access token. The backend
// (app/api/work_inbox.py) iterates every workspace the caller can ACT in
// (role >= accountant), pulls each one's OPEN approval_queue + tax_review_queue
// items INSIDE that workspace's RLS session, and returns ONE merged,
// stalest-first list so a CPA can triage across all clients without switching
// workspaces ~20 times. RBAC + entity-scope (BOLA) are enforced server-side;
// the web layer only passes the token through.
//
// Returns null on any failure so the page can degrade to a soft error state
// (see web/app/clients/inbox/page.tsx).

import { auth } from "@/lib/auth";

const INTERNAL_BASE =
  process.env.INTERNAL_API_BASE_URL ??
  process.env.NEXT_PUBLIC_API_BASE_URL ??
  "http://app:8000";

// ---------------------------------------------------------------------------
// Types — mirror the contract in app/api/work_inbox.py +
// app/metrics/work_inbox.py (WorkInboxItem / build_work_inbox envelope).
// ---------------------------------------------------------------------------

export type WorkInboxItemType = "approval" | "tax_review";

export type WorkInboxItem = {
  id: string;
  workspace_id: string;
  workspace_display_name: string;
  entity_id: string | null;
  item_type: WorkInboxItemType;
  subject_type: string | null;
  title: string;
  amount?: string | null;
  currency_code?: string | null;
  recommended_decision?: string | null;
  recommended_category?: string | null;
  confidence?: string | null;
  severity?: string | null;
  created_at: string | null;
  age_hours: number;
  deep_link: string;
};

export type WorkInboxWorkspaceTotal = {
  workspace_id: string;
  display_name: string;
  count: number;
};

export type WorkInboxTotals = {
  total: number;
  by_item_type: {
    approval: number;
    tax_review: number;
    // The backend keys are stable, but stay permissive in case it grows one.
    [k: string]: number;
  };
  by_workspace: WorkInboxWorkspaceTotal[];
};

export type WorkInboxResponse = {
  items: WorkInboxItem[];
  totals: WorkInboxTotals;
  truncated: boolean;
  dropped: number;
};

// ---------------------------------------------------------------------------
// Server fetcher
// ---------------------------------------------------------------------------

export async function fetchWorkInbox(
  opts: { maxTotal?: number; maxPerWorkspace?: number } = {}
): Promise<WorkInboxResponse | null> {
  const session = await auth();
  if (!session?.accessToken) return null;

  const params = new URLSearchParams();
  if (opts.maxTotal != null) params.set("max_total", String(opts.maxTotal));
  if (opts.maxPerWorkspace != null)
    params.set("max_per_workspace", String(opts.maxPerWorkspace));
  const qs = params.toString();

  try {
    const res = await fetch(
      `${INTERNAL_BASE}/api/me/work-inbox${qs ? "?" + qs : ""}`,
      {
        headers: {
          Authorization: `Bearer ${session.accessToken}`,
          Accept: "application/json"
        },
        cache: "no-store"
      }
    );
    if (!res.ok) return null;
    return (await res.json()) as WorkInboxResponse;
  } catch {
    return null;
  }
}

// ---------------------------------------------------------------------------
// Presentation helpers (pure — safe to import into the client view)
// ---------------------------------------------------------------------------

/** "3d 4h" / "5h" / "just now" from the backend's whole-ish `age_hours`. */
export function formatAge(ageHours: number): string {
  if (!Number.isFinite(ageHours) || ageHours <= 0) return "just now";
  const totalHours = Math.floor(ageHours);
  const days = Math.floor(totalHours / 24);
  const hours = totalHours % 24;
  if (days > 0) return hours > 0 ? `${days}d ${hours}h` : `${days}d`;
  if (hours > 0) return `${hours}h`;
  // < 1h but > 0
  return "<1h";
}
