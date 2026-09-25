// Client helpers for the "Run drafts" launcher (backend: app/api/tax_run_launch.py).
//
// Browser-side fetches go through the /api/workspaces/[...slug] catch-all
// proxy, which attaches the signed-in user's bearer. The backend enforces the
// accountant floor, entity scope and the one-active-run rule; the UI gating
// here is only so people are not shown a button that will 403.

import type { SpecialistRun } from "@/lib/specialist-runs";

/** Roles at or above the backend's "accountant" floor (app/tenancy._ROLE_RANK). */
export const RUN_DRAFTS_ROLES = [
  "accountant",
  "cpa",
  "admin",
  "owner",
  "client_owner",
  "platform_admin"
] as const;

export function canRunDrafts(role: string | undefined | null): boolean {
  return !!role && (RUN_DRAFTS_ROLES as readonly string[]).includes(role);
}

/** agent_token_id the launcher stamps; only these rows can be cancelled in-app. */
export const LAUNCHER_AGENT_ID = "app-launcher";

export type TaxRunPreview = {
  enabled: boolean;
  disabled_reason: string | null;
  backend: string;
  model: string;
  provider: string;
  third_party: boolean;
  specialists: string[];
  data_sent: string[];
  max_runtime_s: number;
  cost_note: string;
  active_run: SpecialistRun | null;
};

export function launchUrl(workspaceId: string, entityId: string, year: number): string {
  return `/api/workspaces/${workspaceId}/entities/${entityId}/tax/${year}/specialist-runs/launch`;
}

export function cancelUrl(workspaceId: string, runId: string): string {
  return `/api/workspaces/${workspaceId}/specialist-runs/${runId}/cancel`;
}

/** Pull a readable message out of a FastAPI error body ({detail: string | {message}}). */
export async function errorMessage(res: Response): Promise<string> {
  try {
    const body = (await res.json()) as { detail?: unknown };
    const d = body.detail;
    if (typeof d === "string") return d;
    if (d && typeof d === "object" && "message" in d) {
      return String((d as { message: unknown }).message);
    }
  } catch {
    // fall through
  }
  return `Request failed (${res.status}).`;
}

export function isActive(run: Pick<SpecialistRun, "status">): boolean {
  return run.status === "queued" || run.status === "running";
}
