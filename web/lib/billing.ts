// Server-side helper for /settings/billing (hosted credit / entitlement status).
//
// Reads the workspace's billing status from the FastAPI backend with the
// signed-in user's Keycloak bearer. Mirrors lib/inference-settings.ts
// (getByokStatus) exactly. The endpoint is being added backend-side; until it
// deploys this returns null and callers degrade to a BYOK-only view rather than
// crashing.

import { auth } from "@/lib/auth";

const INTERNAL_BASE =
  process.env.INTERNAL_API_BASE_URL ??
  process.env.NEXT_PUBLIC_API_BASE_URL ??
  "http://app:8000";

export type BillingStatus = {
  edition: "community" | "enterprise";
  gate_enabled: boolean;
  entitled: boolean;
  byok: boolean;
  can_use_smart: boolean;
  upgrade_url: string | null;
  byok_setup_url: string;
};

export async function getBillingStatus(
  workspaceId: string
): Promise<BillingStatus | null> {
  const session = await auth();
  const token = session?.accessToken;
  if (!token) return null;
  try {
    const res = await fetch(
      `${INTERNAL_BASE}/api/workspaces/${encodeURIComponent(
        workspaceId
      )}/billing`,
      {
        headers: {
          Authorization: `Bearer ${token}`,
          Accept: "application/json",
        },
        cache: "no-store",
      }
    );
    if (!res.ok) return null;
    return (await res.json()) as BillingStatus;
  } catch {
    return null;
  }
}
