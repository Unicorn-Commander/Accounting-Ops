// Server-side fetcher + types for the /specialty-rules page.
//
// Mirrors the FastAPI SpecialtyRulesResponse shape from
// app/api/specialty_rules.py. No-store fetch with the user's Keycloak
// access token; backend enforces RBAC + entity scope.

import { auth } from "@/lib/auth";

const INTERNAL_BASE =
  process.env.INTERNAL_API_BASE_URL ??
  process.env.NEXT_PUBLIC_API_BASE_URL ??
  "http://app:8000";

export type SpecialtyRuleRow = {
  id: string;
  workspace_id: string;
  entity_id: string;
  entity_display_name: string | null;
  profile_id: string;
  profile_name: string;
  profile_active: boolean;
  kind: string;
  params: Record<string, unknown>;
  trigger: string;
  action: string;
  source: string;
  reasoning: string;
  effective_from: string;
  effective_to: string | null;
  active: boolean;
  hits_ytd: number;
};

export type SpecialtyRulesResponse = {
  rows: SpecialtyRuleRow[];
  total: number;
  as_of: string;
};

export async function fetchSpecialtyRules(
  workspaceId: string,
  opts: {
    entityId?: string;
    includeInactive?: boolean;
  } = {}
): Promise<SpecialtyRulesResponse | null> {
  const session = await auth();
  if (!session?.accessToken) return null;

  const params = new URLSearchParams();
  if (opts.entityId) params.set("entity_id", opts.entityId);
  if (opts.includeInactive) params.set("include_inactive", "true");
  const qs = params.toString();

  try {
    const res = await fetch(
      `${INTERNAL_BASE}/api/workspaces/${workspaceId}/specialty-rules${
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
    return (await res.json()) as SpecialtyRulesResponse;
  } catch {
    return null;
  }
}
