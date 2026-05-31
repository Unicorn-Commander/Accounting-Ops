// Server-side fetchers + types for the Sentinel daemon API.
//
// Routes called:
//   GET  /api/workspaces/{wid}/sentinel/config
//   POST /api/workspaces/{wid}/sentinel/config
//   GET  /api/workspaces/{wid}/sentinel/findings
//   POST /api/workspaces/{wid}/sentinel/findings/{id}/acknowledge
//   GET  /api/workspaces/{wid}/sentinel/check-runs
//   GET  /api/workspaces/{wid}/sentinel/scheduled-actions
//   POST /api/workspaces/{wid}/sentinel/run

import { auth } from "@/lib/auth";

const INTERNAL_BASE =
  process.env.INTERNAL_API_BASE_URL ??
  process.env.NEXT_PUBLIC_API_BASE_URL ??
  "http://app:8000";

// ---------------------------------------------------------------------------
// Types
// ---------------------------------------------------------------------------

export type SentinelAutonomyLevel =
  | "off"
  | "watch_only"
  | "draft_and_flag"
  | "fully_autonomous";

export type SentinelConfig = {
  workspace_id: string;
  autonomy_level: SentinelAutonomyLevel;
  check_interval_minutes: number;
  max_actions_per_day: number;
  enabled_checks: string[];
  notify_owners: boolean;
  notify_accountants: boolean;
  last_run_at: string | null;
  all_check_types: string[];
};

export type SentinelFinding = {
  id: string;
  entity_id: string | null;
  entity_display_name: string | null;
  check_type: string;
  severity: "info" | "low" | "medium" | "high" | "critical";
  title: string;
  summary: string;
  details: Record<string, unknown> | null;
  action_type: string | null;
  action_ref_id: string | null;
  status: "open" | "acknowledged" | "resolved" | "dismissed";
  check_run_id: string | null;
  decision_audit_id: string | null;
  acknowledged_by_uc_uid: string | null;
  acknowledged_at: string | null;
  created_at: string;
};

export type SentinelCheckRun = {
  id: string;
  check_type: string;
  cycle_id: string;
  started_at: string;
  finished_at: string | null;
  duration_ms: number | null;
  status: "running" | "completed" | "failed" | "skipped";
  findings_count: number;
  actions_taken: number;
  summary: string | null;
  error_summary: string | null;
};

export type SentinelScheduledAction = {
  id: string;
  entity_id: string | null;
  action_type: string;
  scheduled_for: string;
  payload: Record<string, unknown>;
  status: "pending" | "executed" | "cancelled" | "failed";
  execution_notes: string | null;
  created_at: string;
  executed_at: string | null;
};

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

async function authedFetch(path: string, init?: RequestInit): Promise<Response | null> {
  const session = await auth();
  if (!session?.accessToken) return null;
  try {
    return await fetch(`${INTERNAL_BASE}${path}`, {
      ...init,
      headers: {
        ...(init?.headers ?? {}),
        Authorization: `Bearer ${session.accessToken}`,
        Accept: "application/json"
      },
      cache: "no-store"
    });
  } catch {
    return null;
  }
}

// ---------------------------------------------------------------------------
// Fetchers
// ---------------------------------------------------------------------------

export async function getSentinelConfig(workspaceId: string): Promise<SentinelConfig | null> {
  const res = await authedFetch(`/api/workspaces/${workspaceId}/sentinel/config`);
  if (!res || !res.ok) return null;
  return (await res.json()) as SentinelConfig;
}

export async function listFindings(
  workspaceId: string,
  opts: { status?: string; checkType?: string; days?: number; limit?: number } = {}
): Promise<{ items: SentinelFinding[]; total: number } | null> {
  const params = new URLSearchParams();
  params.set("status", opts.status ?? "open");
  if (opts.checkType) params.set("check_type", opts.checkType);
  params.set("days", String(opts.days ?? 30));
  params.set("limit", String(opts.limit ?? 100));
  const res = await authedFetch(
    `/api/workspaces/${workspaceId}/sentinel/findings?${params}`
  );
  if (!res || !res.ok) return null;
  return await res.json();
}

export async function listCheckRuns(
  workspaceId: string,
  opts: { days?: number; limit?: number } = {}
): Promise<{ items: SentinelCheckRun[]; cycles: number } | null> {
  const params = new URLSearchParams();
  params.set("days", String(opts.days ?? 7));
  params.set("limit", String(opts.limit ?? 200));
  const res = await authedFetch(
    `/api/workspaces/${workspaceId}/sentinel/check-runs?${params}`
  );
  if (!res || !res.ok) return null;
  return await res.json();
}

export async function listScheduledActions(
  workspaceId: string,
  opts: { status?: string; limit?: number } = {}
): Promise<{ items: SentinelScheduledAction[] } | null> {
  const params = new URLSearchParams();
  params.set("status", opts.status ?? "pending");
  params.set("limit", String(opts.limit ?? 100));
  const res = await authedFetch(
    `/api/workspaces/${workspaceId}/sentinel/scheduled-actions?${params}`
  );
  if (!res || !res.ok) return null;
  return await res.json();
}
