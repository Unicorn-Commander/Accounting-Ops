// Server-side fetch helper for the backend FastAPI service.
// Uses INTERNAL_API_BASE_URL inside docker network, falls back to public URL.

import { auth } from "@/lib/auth";

const INTERNAL_BASE =
  process.env.INTERNAL_API_BASE_URL ??
  process.env.NEXT_PUBLIC_API_BASE_URL ??
  "http://app:8000";

export type Workspace = {
  id: string;
  slug: string;
  display_name: string;
  role: "owner" | "admin" | "accountant" | "viewer";
  isolation_tier: "shared" | "premium" | "enterprise";
  hipaa_protected: boolean;
  entity_count: number;
  last_active_at: string | null;
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
