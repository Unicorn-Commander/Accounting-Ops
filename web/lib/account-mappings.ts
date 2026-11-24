// Server-side fetch for the account-mapping UI.

import { auth } from "@/lib/auth";

const INTERNAL_BASE =
  process.env.INTERNAL_API_BASE_URL ??
  process.env.NEXT_PUBLIC_API_BASE_URL ??
  "http://app:8000";

export type LiveAccount = {
  source: string;
  external_account_id: string;
  external_org: string;
  external_account_name: string;
  currency: string;
  balance: string;
  mapped: boolean;
  mapping_id?: string;
  entity_id?: string;
  entity_display_name?: string;
  entity_slug?: string;
  auto_categorize?: boolean;
  label?: string | null;
};

export type EntityRow = {
  id: string;
  slug: string;
  display_name: string;
  kind: string;
};

export type AccountMappingsResponse = {
  live_accounts: LiveAccount[];
  mappings: Array<Record<string, unknown>>;
  entities: EntityRow[];
  unmapped_count: number;
};

export type AccountMappingsResult =
  | ({ kind: "ok" } & AccountMappingsResponse)
  | { kind: "forbidden" }
  | { kind: "unavailable" };

export async function getAccountMappings(
  workspaceId: string
): Promise<AccountMappingsResult> {
  const session = await auth();
  if (!session?.accessToken) return { kind: "unavailable" };
  try {
    const res = await fetch(`${INTERNAL_BASE}/api/workspaces/${workspaceId}/account-mappings`, {
      headers: {
        Authorization: `Bearer ${session.accessToken}`,
        Accept: "application/json"
      },
      cache: "no-store"
    });
    // AO111-R2-F5: the GET needs accountant; a 403 is "not for your role",
    // never "0 unmapped accounts".
    if (res.status === 403) return { kind: "forbidden" };
    if (!res.ok) return { kind: "unavailable" };
    return { kind: "ok", ...((await res.json()) as AccountMappingsResponse) };
  } catch {
    return { kind: "unavailable" };
  }
}
