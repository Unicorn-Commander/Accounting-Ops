// Server-side helpers for the Stripe connector status.

import { auth } from "@/lib/auth";

const INTERNAL_BASE =
  process.env.INTERNAL_API_BASE_URL ??
  process.env.NEXT_PUBLIC_API_BASE_URL ??
  "http://app:8000";

export type StripeConnectorStatus =
  | { configured: false }
  | {
      configured: true;
      account_id: string | null;
      business_name: string | null;
      environment: "live" | "test" | null;
      key_display: string | null;
      webhook_secret_configured: boolean;
      default_revenue_account: string | null;
      entity_id: string | null;
      granted_at_iso: string | null;
      last_sync_at_iso: string | null;
      last_sync_outcome: string | null;
      recent_imports_count: number;
      warnings: string[];
    };

export async function getStripeStatus(
  workspaceId: string
): Promise<StripeConnectorStatus | null> {
  const session = await auth();
  if (!session?.accessToken) return null;
  try {
    const res = await fetch(
      `${INTERNAL_BASE}/api/workspaces/${workspaceId}/connectors/stripe/status`,
      {
        headers: {
          Authorization: `Bearer ${session.accessToken}`,
          Accept: "application/json"
        },
        cache: "no-store"
      }
    );
    if (!res.ok) return null;
    return (await res.json()) as StripeConnectorStatus;
  } catch {
    return null;
  }
}
