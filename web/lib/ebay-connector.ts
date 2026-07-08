// Server-side helpers for the eBay Finances connector status.

import { auth } from "@/lib/auth";

const INTERNAL_BASE =
  process.env.INTERNAL_API_BASE_URL ??
  process.env.NEXT_PUBLIC_API_BASE_URL ??
  "http://app:8000";

export type EbayConnectorStatus =
  | { configured: false }
  | {
      configured: true;
      seller_handle: string | null;
      environment: "production" | "sandbox" | null;
      granted_scopes: string[];
      granted_at_iso: string | null;
      access_token_expires_at_iso: string | null;
      refresh_token_expires_at_iso: string | null;
      entity_id: string | null;
      last_sync_at_iso: string | null;
      last_sync_outcome: string | null;
      recent_imports_count: number;
    };

export async function getEbayStatus(
  workspaceId: string
): Promise<EbayConnectorStatus | null> {
  const session = await auth();
  if (!session?.accessToken) return null;
  try {
    const res = await fetch(
      `${INTERNAL_BASE}/api/workspaces/${workspaceId}/connectors/ebay/status`,
      {
        headers: {
          Authorization: `Bearer ${session.accessToken}`,
          Accept: "application/json"
        },
        cache: "no-store"
      }
    );
    if (!res.ok) return null;
    return (await res.json()) as EbayConnectorStatus;
  } catch {
    return null;
  }
}
