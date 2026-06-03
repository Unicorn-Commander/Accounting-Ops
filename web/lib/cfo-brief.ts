// Server-side client for the CFO-brief assembler endpoint.
//
// Mirrors lib/open-items.ts + lib/cash-position.ts: a no-store fetch to
// FastAPI using the signed-in user's Keycloak access token. The backend
// deterministically synthesizes open-items + cash + sentinel findings into a
// ranked, plain-language top-N payload (NOT a live LLM call). RBAC + entity
// scope are enforced server-side; the web layer only passes the token through.
//
// Returns null on any failure so the dashboard degrades to its existing
// content (see web/app/dashboard/page.tsx).

import { auth } from "@/lib/auth";

const INTERNAL_BASE =
  process.env.INTERNAL_API_BASE_URL ??
  process.env.NEXT_PUBLIC_API_BASE_URL ??
  "http://app:8000";

// ---------------------------------------------------------------------------
// Types — mirror the FastAPI response models in app/api/cfo_brief.py
// ---------------------------------------------------------------------------

export type CfoBriefPriority = {
  id: string;
  kind: "time_sensitive" | "decision" | "document" | "filing" | "finding";
  severity: "critical" | "high" | "medium" | "low" | "info";
  headline: string;
  detail: string | null;
  action_href: string;
  action_label: string;
  score: number;
};

export type CfoBriefCash = {
  configured: boolean;
  headline: string;
  detail: string | null;
  assets_total: string | null;
  liabilities_total: string | null;
  net_total: string | null;
  months_runway: string | null;
  tone: "credit" | "debit" | "pending" | "ink";
};

export type CfoBriefResponse = {
  workspace_id: string;
  entity_id: string | null;
  as_of: string;
  priorities: CfoBriefPriority[];
  action_required_total: number;
  cash: CfoBriefCash;
  summary_line: string;
  // Cold start: no bank connected AND no posted entries (brand-new user).
  // The card shows "here's how to begin" instead of "you're all caught up".
  onboarding?: boolean;
  ask_cfo_href: string;
  sources_ok: Record<string, boolean>;
};

// ---------------------------------------------------------------------------
// Server fetcher
// ---------------------------------------------------------------------------

export async function fetchCfoBrief(
  workspaceId: string,
  opts: { entityId?: string } = {}
): Promise<CfoBriefResponse | null> {
  const session = await auth();
  if (!session?.accessToken) return null;

  const params = new URLSearchParams();
  if (opts.entityId) params.set("entity_id", opts.entityId);
  const qs = params.toString();

  try {
    const res = await fetch(
      `${INTERNAL_BASE}/api/workspaces/${workspaceId}/cfo-brief${qs ? "?" + qs : ""}`,
      {
        headers: {
          Authorization: `Bearer ${session.accessToken}`,
          Accept: "application/json"
        },
        cache: "no-store"
      }
    );
    if (!res.ok) return null;
    return (await res.json()) as CfoBriefResponse;
  } catch {
    return null;
  }
}
