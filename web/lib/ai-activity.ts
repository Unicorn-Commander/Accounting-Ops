// Server-side client for the /ai-activity timeline + undo + challenge.
//
// Mirrors lib/open-items.ts: no-store fetch to FastAPI using the
// signed-in user's Keycloak access token. The backend enforces RBAC +
// entity scope; the web layer only passes the token through.

import { auth } from "@/lib/auth";

const INTERNAL_BASE =
  process.env.INTERNAL_API_BASE_URL ??
  process.env.NEXT_PUBLIC_API_BASE_URL ??
  "http://app:8000";

// ---------------------------------------------------------------------------
// Types — mirror the FastAPI AIActivityRow / AIActivityResponse
// ---------------------------------------------------------------------------

export type AIActivityArtifact = {
  kind: string;
  label: string;
  queue_item_id?: string;
  approval_id?: string;
  journal_entry_id?: string;
  run_id?: string;
  tax_year?: number;
  entity_id?: string;
};

export type AIActivityRow = {
  id: string;
  workspace_id: string;
  entity_id: string;
  entity_display_name: string | null;
  agent_id: string;
  agent_session_id: string | null;
  tool_name: string;
  action_label: string;
  target: Record<string, unknown>;
  outcome: string;
  auto_applied: boolean;
  reasoning_trace: string;
  request_payload: Record<string, unknown> | null;
  response_payload: Record<string, unknown> | null;
  artifact: AIActivityArtifact | null;
  reversible: boolean;
  reverse_kind: string | null;
  reverse_window_hours: number;
  undone_at: string | null;
  undone_by: string | null;
  challenged_at: string | null;
  challenged_by: string | null;
  challenge_memo_id: string | null;
  created_at: string;
  latency_ms: number | null;
};

export type AIActivityResponse = {
  rows: AIActivityRow[];
  as_of: string;
  total_returned: number;
};

// ---------------------------------------------------------------------------
// Server fetcher
// ---------------------------------------------------------------------------

export async function fetchAIActivity(
  workspaceId: string,
  opts: {
    entityId?: string;
    since?: string;
    until?: string;
    limit?: number;
    agent?: string;
    action?: string;
  } = {}
): Promise<AIActivityResponse | null> {
  const session = await auth();
  if (!session?.accessToken) return null;

  const params = new URLSearchParams();
  if (opts.entityId) params.set("entity_id", opts.entityId);
  if (opts.since) params.set("since", opts.since);
  if (opts.until) params.set("until", opts.until);
  if (opts.limit) params.set("limit", String(opts.limit));
  if (opts.agent) params.set("agent", opts.agent);
  if (opts.action) params.set("action", opts.action);
  const qs = params.toString();

  try {
    const res = await fetch(
      `${INTERNAL_BASE}/api/workspaces/${workspaceId}/ai-activity${
        qs ? "?" + qs : ""
      }`,
      {
        headers: {
          Authorization: `Bearer ${session.accessToken}`,
          Accept: "application/json"
        },
        cache: "no-store"
      }
    );
    if (!res.ok) return null;
    return (await res.json()) as AIActivityResponse;
  } catch {
    return null;
  }
}
