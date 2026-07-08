// Server-side client for the /open-items aggregator endpoint.
//
// Mirrors lib/tax-review-queue.ts: a no-store fetch to FastAPI using the
// signed-in user's Keycloak access token. Backend enforces RBAC + entity
// scope. The web layer only passes the token through.

import { auth } from "@/lib/auth";

const INTERNAL_BASE =
  process.env.INTERNAL_API_BASE_URL ??
  process.env.NEXT_PUBLIC_API_BASE_URL ??
  "http://app:8000";

// ---------------------------------------------------------------------------
// Types — mirror the FastAPI response models
// ---------------------------------------------------------------------------

export type QueueRow = {
  id: string;
  entity_id: string;
  entity_display_name: string | null;
  entity_slug: string | null;
  tax_year: number;
  item_type: string;
  form_reference: string | null;
  summary: string;
  confidence: "low" | "medium" | "high" | null;
  status: string;
  submitter_kind: "agent" | "human";
  submitter_id: string;
  created_at: string;
};

export type ResolvedRow = QueueRow & {
  reviewer_uc_uid: string | null;
  resolution_notes: string | null;
  resolved_at: string;
};

export type DocChecklistItem = {
  key: string;
  name: string;
  description: string;
  where_to_get: string;
  where_to_get_url: string | null;
  needed_for: string;
  tax_years: number[];
  priority: "time_sensitive" | "high" | "medium" | "low";
  status: "pending" | "received";
  received_at: string | null;
  marked_by: string | null;
};

export type DecisionItem = {
  key: string;
  question: string;
  recommendation: string;
  rationale: string;
  tax_years: number[];
  form_reference_tag: string;
  status: "pending" | "resolved";
  verdict: "accept" | "other" | null;
  notes: string | null;
  resolved_at: string | null;
  decided_by: string | null;
};

export type OpenItemsResponse = {
  workspace_id: string;
  entity_id: string | null;
  as_of: string;
  counts: {
    time_sensitive: number;
    documents_pending: number;
    documents_received: number;
    decisions_pending: number;
    decisions_decided: number;
    waiting_on_cpa: number;
    waiting_on_attorney: number;
    informational: number;
    resolved_this_week: number;
    action_required: number;
  };
  time_sensitive: QueueRow[];
  documents: DocChecklistItem[];
  decisions: DecisionItem[];
  waiting_on_cpa: QueueRow[];
  waiting_on_attorney: QueueRow[];
  resolved_this_week: ResolvedRow[];
};

// ---------------------------------------------------------------------------
// Server fetcher
// ---------------------------------------------------------------------------

export async function fetchOpenItems(
  workspaceId: string,
  opts: { entityId?: string; taxYear?: number } = {}
): Promise<OpenItemsResponse | null> {
  const session = await auth();
  if (!session?.accessToken) return null;

  const params = new URLSearchParams();
  if (opts.entityId) params.set("entity_id", opts.entityId);
  if (opts.taxYear) params.set("tax_year", String(opts.taxYear));
  const qs = params.toString();

  try {
    const res = await fetch(
      `${INTERNAL_BASE}/api/workspaces/${workspaceId}/open-items${qs ? "?" + qs : ""}`,
      {
        headers: {
          Authorization: `Bearer ${session.accessToken}`,
          Accept: "application/json"
        },
        cache: "no-store"
      }
    );
    if (!res.ok) return null;
    return (await res.json()) as OpenItemsResponse;
  } catch {
    return null;
  }
}
