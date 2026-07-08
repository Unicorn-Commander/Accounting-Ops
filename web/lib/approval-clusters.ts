// Types + fetcher for the cluster-card UX endpoints.
//
// Backed by:
//   GET  /api/workspaces/{wid}/approvals/clusters
//   POST /api/workspaces/{wid}/approvals/bulk-decide
//
// Server-side fetcher mirrors lib/approvals.ts (no-store, signed-in user token).

import { auth } from "@/lib/auth";
import type { ApprovalClustersResponse } from "@/lib/approval-cluster-format";
export type {
  ApprovalCluster,
  ApprovalClustersResponse,
  ApprovalItem,
  ClusterConfidence,
  ClusterLimits,
  ClusterTotals
} from "@/lib/approval-cluster-format";
export {
  confidenceLabel,
  confidenceTone,
  flagLabel,
  formatUsd,
  formatUsdPrecise
} from "@/lib/approval-cluster-format";

const INTERNAL_BASE =
  process.env.INTERNAL_API_BASE_URL ??
  process.env.NEXT_PUBLIC_API_BASE_URL ??
  "http://app:8000";

export async function getApprovalClusters(
  workspaceId: string,
  opts: { entityId?: string } = {}
): Promise<ApprovalClustersResponse | null> {
  const session = await auth();
  if (!session?.accessToken) return null;

  const params = new URLSearchParams();
  if (opts.entityId) params.set("entity_id", opts.entityId);

  try {
    const res = await fetch(
      `${INTERNAL_BASE}/api/workspaces/${workspaceId}/approvals/clusters?${params}`,
      {
        headers: {
          Authorization: `Bearer ${session.accessToken}`,
          Accept: "application/json"
        },
        cache: "no-store"
      }
    );
    if (!res.ok) return null;
    return (await res.json()) as ApprovalClustersResponse;
  } catch {
    return null;
  }
}
