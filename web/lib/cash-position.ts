// Real-time cash position fetch. Server-side, hits the FastAPI backend with
// the caller's bearer token; backend fans out to SimpleFIN.

import { auth } from "@/lib/auth";
import type { CashPosition } from "@/lib/cash-format";
export type { CashAccount, CashPosition } from "@/lib/cash-format";
export { formatUSD } from "@/lib/cash-format";

const INTERNAL_BASE =
  process.env.INTERNAL_API_BASE_URL ??
  process.env.NEXT_PUBLIC_API_BASE_URL ??
  "http://app:8000";

export async function getCashPosition(
  workspaceId: string,
  entityId?: string
): Promise<CashPosition | null> {
  const session = await auth();
  if (!session?.accessToken) return null;
  try {
    const params = entityId ? `?entity_id=${encodeURIComponent(entityId)}` : "";
    const res = await fetch(
      `${INTERNAL_BASE}/api/workspaces/${workspaceId}/cash-position${params}`,
      {
        headers: {
          Authorization: `Bearer ${session.accessToken}`,
          Accept: "application/json"
        },
        cache: "no-store"
      }
    );
    if (!res.ok) return null;
    return (await res.json()) as CashPosition;
  } catch {
    return null;
  }
}
