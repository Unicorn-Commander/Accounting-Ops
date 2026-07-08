// Server-side fetcher for chart of accounts.
//
// Why server-side: the Next.js client-side proxy at /api/coa/[wid]/[eid] is
// unreachable in production because Traefik's `PathPrefix('/api')` rule
// routes ALL /api/* requests to the app backend, which doesn't have that
// exact path (its endpoint is /api/workspaces/{wid}/coa?entity_id=X). The
// browser fetch gets a 404. Server-side fetching from RSC works because we
// can attach the NextAuth session bearer directly via auth().
import { auth } from "@/lib/auth";

const INTERNAL_BASE =
  process.env.INTERNAL_API_BASE_URL ??
  process.env.NEXT_PUBLIC_API_BASE_URL ??
  "http://app:8000";

export type CoaAccount = {
  id: string;
  account_number: string;
  account_name: string;
  canonical_form: string | null;
  account_type: string;
  normal_balance: string;
  linked_external_source: string | null;
  linked_external_account_id: string | null;
  is_active: boolean;
  is_system: boolean;
  notes: string | null;
};

export type CoaResponse = {
  entity_id: string;
  accounts: CoaAccount[];
};

/** Fetch the chart of accounts for an entity, server-side. */
export async function fetchChartOfAccounts(
  workspaceId: string,
  entityId: string,
): Promise<CoaResponse> {
  const session = await auth();
  if (!session?.accessToken) {
    return { entity_id: entityId, accounts: [] };
  }
  try {
    const res = await fetch(
      `${INTERNAL_BASE}/api/workspaces/${workspaceId}/coa?entity_id=${entityId}`,
      {
        headers: {
          Authorization: `Bearer ${session.accessToken}`,
          Accept: "application/json",
        },
        cache: "no-store",
      },
    );
    if (!res.ok) {
      return { entity_id: entityId, accounts: [] };
    }
    return (await res.json()) as CoaResponse;
  } catch {
    return { entity_id: entityId, accounts: [] };
  }
}
