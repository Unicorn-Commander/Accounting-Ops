// Server-side helpers for /settings/pats. Calls the FastAPI backend with the
// signed-in user's Keycloak bearer (NOT a PAT — PATs can't mint more PATs).

import { auth } from "@/lib/auth";

const INTERNAL_BASE =
  process.env.INTERNAL_API_BASE_URL ??
  process.env.NEXT_PUBLIC_API_BASE_URL ??
  "http://app:8000";

export type PATState = "active" | "expired" | "revoked";

export type PersonalAccessToken = {
  id: string;
  name: string;
  prefix: string;
  scopes: string[];
  workspace_scope: string[] | null;
  created_at: string | null;
  last_used_at: string | null;
  expires_at: string | null;
  revoked_at: string | null;
  state: PATState;
};

export type PATWorkspace = {
  id: string;
  slug: string;
  display_name: string;
  role: string;
};

export type PATMeta = {
  all_scopes: string[];
  owner_allowed_scopes: string[];
  ttl_choices_days: number[];
  default_ttl_days: number;
  max_ttl_days: number;
  max_active_pats: number;
  workspaces: PATWorkspace[];
};

async function bearer(): Promise<string | null> {
  const session = await auth();
  return session?.accessToken ?? null;
}

async function getJson<T>(path: string): Promise<T | null> {
  const token = await bearer();
  if (!token) return null;
  try {
    const res = await fetch(`${INTERNAL_BASE}${path}`, {
      headers: {
        Authorization: `Bearer ${token}`,
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

export async function listMyPATs(): Promise<PersonalAccessToken[]> {
  return (await getJson<PersonalAccessToken[]>("/api/me/personal-access-tokens")) ?? [];
}

export async function getPATMeta(): Promise<PATMeta | null> {
  return getJson<PATMeta>("/api/me/personal-access-tokens/_meta");
}
