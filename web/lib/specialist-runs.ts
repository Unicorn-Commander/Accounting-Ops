// Server-side client for the specialist-runs observability endpoints.
//
// Mirrors lib/tax-review-queue.ts: a no-store fetch to the FastAPI backend
// using the signed-in user's Keycloak access token. The backend enforces
// RBAC (viewer floor to read, accountant floor to start/finish).

import { auth } from "@/lib/auth";

const INTERNAL_BASE =
  process.env.INTERNAL_API_BASE_URL ??
  process.env.NEXT_PUBLIC_API_BASE_URL ??
  "http://app:8000";

// ---------------------------------------------------------------------------
// Types — mirror the backend specialist_run schema
// ---------------------------------------------------------------------------

export type SpecialistRunStatus =
  | "running"
  | "completed"
  | "failed"
  | "cancelled";

export type SpecialistRunMetrics = {
  input_tokens: number;
  output_tokens: number;
  total_tool_calls: number;
  mcp_tool_breakdown?: Record<string, number>;
};

export type SpecialistRunOutputSummary = {
  output_file?: string;
  output_bytes?: number;
  queue_rows_created?: number;
  [key: string]: unknown;
};

export type SpecialistRunInputSummary = {
  workspace_id?: string;
  entity_id?: string;
  tax_year?: number;
  args?: Record<string, unknown>;
  [key: string]: unknown;
};

export type SpecialistRun = {
  id: string;
  workspace_id: string;
  entity_id: string;
  entity_display_name: string | null;
  entity_slug: string | null;
  tax_year: number;
  specialist_id: string;
  agent_token_id: string | null;
  invoker_uc_uid: string;
  backend: string;
  model: string;
  // endpoint_url intentionally omitted — stripped by backend
  started_at: string;
  finished_at: string | null;
  duration_ms: number | null;
  status: SpecialistRunStatus;
  input_summary: SpecialistRunInputSummary | null;
  output_summary: SpecialistRunOutputSummary | null;
  metrics: SpecialistRunMetrics | null;
  error_summary: string | null;
  trace_id: string | null;
};

export type SpecialistRunsResponse = {
  items: SpecialistRun[];
  total: number;
};

// ---------------------------------------------------------------------------
// Server-side fetchers — call the FastAPI backend directly with user token.
// ---------------------------------------------------------------------------

export async function fetchSpecialistRuns(
  workspaceId: string,
  opts: {
    entityId?: string;
    taxYear?: number;
    specialistId?: string;
    status?: string;
    limit?: number;
    offset?: number;
  } = {}
): Promise<SpecialistRunsResponse> {
  const session = await auth();
  if (!session?.accessToken) {
    return { items: [], total: 0 };
  }

  const params = new URLSearchParams();
  if (opts.entityId) params.set("entity_id", opts.entityId);
  if (opts.taxYear) params.set("tax_year", String(opts.taxYear));
  if (opts.specialistId) params.set("specialist_id", opts.specialistId);
  if (opts.status) params.set("status", opts.status);
  if (opts.limit) params.set("limit", String(opts.limit));
  if (opts.offset) params.set("offset", String(opts.offset));

  try {
    const res = await fetch(
      `${INTERNAL_BASE}/api/workspaces/${workspaceId}/specialist-runs?${params}`,
      {
        headers: {
          Authorization: `Bearer ${session.accessToken}`,
          Accept: "application/json"
        },
        cache: "no-store"
      }
    );
    if (!res.ok) return { items: [], total: 0 };
    return (await res.json()) as SpecialistRunsResponse;
  } catch {
    return { items: [], total: 0 };
  }
}

export async function fetchSpecialistRun(
  workspaceId: string,
  id: string
): Promise<SpecialistRun | null> {
  const session = await auth();
  if (!session?.accessToken) return null;

  try {
    const res = await fetch(
      `${INTERNAL_BASE}/api/workspaces/${workspaceId}/specialist-runs/${id}`,
      {
        headers: {
          Authorization: `Bearer ${session.accessToken}`,
          Accept: "application/json"
        },
        cache: "no-store"
      }
    );
    if (!res.ok) return null;
    return (await res.json()) as SpecialistRun;
  } catch {
    return null;
  }
}
