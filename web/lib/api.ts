// Server-side fetch helper for the backend FastAPI service.
// Uses INTERNAL_API_BASE_URL inside docker network, falls back to public URL.

import { auth } from "@/lib/auth";

const INTERNAL_BASE =
  process.env.INTERNAL_API_BASE_URL ??
  process.env.NEXT_PUBLIC_API_BASE_URL ??
  "http://app:8000";

export type WorkspaceRole =
  | "platform_admin"
  | "owner"
  | "client_owner"
  | "admin"
  | "cpa"
  | "accountant"
  | "bookkeeper"
  | "client_user"
  | "viewer";

export type Workspace = {
  id: string;
  slug: string;
  display_name: string;
  role: WorkspaceRole;
  isolation_tier: "shared" | "premium" | "enterprise";
  hipaa_protected: boolean;
  entity_count: number;
  last_active_at: string | null;
};

export type Member = {
  membership_id: string;
  user_id: string;
  uc_uid: string;
  email: string | null;
  display_name: string | null;
  role: WorkspaceRole;
  entity_scope: string[] | null;
  invited_by_uc_uid: string | null;
  created_at: string;
  last_active_at: string | null;
};

export type Invitation = {
  id: string;
  email: string;
  role: WorkspaceRole;
  entity_scope: string[] | null;
  expires_at: string;
  accepted_at: string | null;
  cancelled_at: string | null;
  resent_count: number;
  last_resent_at: string | null;
  created_by_uc_uid: string;
  created_at: string;
};

export type ClientsDashboard = {
  persona: "platform_admin" | "cpa" | "client";
  workspaces: (Workspace & {
    open_items_count: number;
    entity_scope?: string[] | null;
  })[];
  total_workspaces: number;
  total_open_items: number;
};

export type TriageRoute =
  | "deadline_soon"
  | "needs_cpa"
  | "waiting_on_client"
  | "stale"
  | "ok";

export type TriageWorkspace = Workspace & {
  open_items_count: number;
  needs_cpa_count: number;
  waiting_on_client_count: number;
  upcoming_deadline: string | null;
  days_to_deadline: number | null;
  last_client_activity_at: string | null;
  suggested_route: TriageRoute;
  entity_scope?: string[] | null;
};

export type ClientsTriage = {
  persona: "platform_admin" | "cpa" | "client";
  workspaces: TriageWorkspace[];
  total_workspaces: number;
  total_open_items: number;
  total_needs_cpa: number;
  total_waiting_on_client: number;
};

export type Entity = {
  id: string;
  slug: string;
  display_name: string;
  legal_name: string | null;
  kind: string;
  country_code: string;
  functional_currency: string;
  hipaa_protected: boolean;
  status: string;
  created_at: string;
};

async function apiFetch<T>(path: string): Promise<T | null> {
  const session = await auth();
  if (!session?.accessToken) return null;
  try {
    const res = await fetch(`${INTERNAL_BASE}${path}`, {
      headers: {
        Authorization: `Bearer ${session.accessToken}`,
        Accept: "application/json"
      },
      cache: "no-store"
    });
    if (!res.ok) return null;
    return (await res.json()) as T;
  } catch {
    return null;
  }
}

export async function getMyWorkspaces(): Promise<Workspace[]> {
  return (await apiFetch<Workspace[]>("/api/me/workspaces")) ?? [];
}

export async function getWorkspaceEntities(workspaceId: string): Promise<Entity[]> {
  return (
    (await apiFetch<Entity[]>(`/api/workspaces/${workspaceId}/entities`)) ?? []
  );
}

export async function getWorkspaceMembers(
  workspaceId: string
): Promise<Member[]> {
  return (
    (await apiFetch<Member[]>(`/api/workspaces/${workspaceId}/members`)) ?? []
  );
}

export async function getWorkspaceInvitations(
  workspaceId: string,
  includeExpired = false
): Promise<Invitation[]> {
  const qs = includeExpired ? "?include_expired=true" : "";
  return (
    (await apiFetch<Invitation[]>(
      `/api/workspaces/${workspaceId}/invitations${qs}`
    )) ?? []
  );
}

export async function getClientsDashboard(): Promise<ClientsDashboard | null> {
  return await apiFetch<ClientsDashboard>("/api/me/clients");
}

export async function getClientsTriage(): Promise<ClientsTriage | null> {
  return await apiFetch<ClientsTriage>("/api/me/clients/triage");
}

export type CpaRecentActivityItem = {
  id: string;
  workspace_id: string;
  workspace_name: string;
  tool: string;
  outcome: string;
  model_version: string | null;
  created_at: string;
  /** Deep-link path the UI should push to AFTER switching the workspace
   *  cookie. Backend computes this from the tool name. */
  deep_link_path?: string | null;
};

export async function getCpaRecentActivity(
  days = 30
): Promise<{ items: CpaRecentActivityItem[]; days: number } | null> {
  return await apiFetch<{ items: CpaRecentActivityItem[]; days: number }>(
    `/api/me/cpa-recent-activity?days=${days}`
  );
}
