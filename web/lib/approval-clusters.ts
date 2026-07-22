// Types + fetcher for the cluster-card UX endpoints.
//
// Backed by:
//   GET  /api/workspaces/{wid}/approvals/clusters
//   POST /api/workspaces/{wid}/approvals/bulk-decide
//
// Server-side fetcher mirrors lib/approvals.ts (no-store, signed-in user token).

import { auth } from "@/lib/auth";

const INTERNAL_BASE =
  process.env.INTERNAL_API_BASE_URL ??
  process.env.NEXT_PUBLIC_API_BASE_URL ??
  "http://app:8000";

// -----------------------------------------------------------------------
// Types
// -----------------------------------------------------------------------

export type ClusterConfidence = "high" | "medium" | "low";

export type ApprovalItem = {
  approval_id: string;
  recommendation_id: string;
  entity_id: string;
  vendor: string;
  payee: string | null;
  memo: string | null;
  category_suggested: string | null;
  amount_cents: number;
  amount_usd: number;
  date: string | null;
  currency_code: string;
  confidence: ClusterConfidence;
  rationale: string | null;
  approval_created_at: string | null;
  flags: string[]; // [] = approvable; non-empty = excluded from bulk
  source_external_id: string | null;
  materiality_tier: string | null;
};

export type ApprovalCluster = {
  cluster_id: string;
  vendor_signature: string;
  category_suggested: string;
  count: number;
  approvable_count: number;
  flagged_count: number;
  total_amount_usd: number;
  amount_range: [number, number];
  date_range: [string | null, string | null];
  confidence: ClusterConfidence;
  items: ApprovalItem[]; // up to CLUSTER_SAMPLE_SIZE (5)
  remainder_count: number;
  flagged_items: ApprovalItem[];
};

export type ClusterTotals = {
  total_pending: number;
  in_clusters: number;
  singletons: number;
  approvable_pending: number;
  flagged_pending: number;
  decided_today: number;
};

export type ClusterLimits = {
  max_bulk_decide_items: number;
  cluster_sample_size: number;
};

export type ApprovalClustersResponse = {
  clusters: ApprovalCluster[];
  singletons: ApprovalItem[];
  totals: ClusterTotals;
  limits: ClusterLimits;
};

// -----------------------------------------------------------------------
// Server-side fetcher (used by the page server component)
// -----------------------------------------------------------------------

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

// -----------------------------------------------------------------------
// Helpers
// -----------------------------------------------------------------------

export function formatUsd(usd: number): string {
  if (!Number.isFinite(usd) || usd === 0) return "$0";
  const sign = usd < 0 ? "-" : "";
  return (
    sign +
    "$" +
    Math.abs(usd).toLocaleString("en-US", {
      minimumFractionDigits: usd >= 100 ? 0 : 2,
      maximumFractionDigits: usd >= 100 ? 0 : 2
    })
  );
}

export function formatUsdPrecise(usd: number): string {
  if (!Number.isFinite(usd)) return "—";
  const sign = usd < 0 ? "-" : "";
  return (
    sign +
    "$" +
    Math.abs(usd).toLocaleString("en-US", {
      minimumFractionDigits: 2,
      maximumFractionDigits: 2
    })
  );
}

export function confidenceTone(c: ClusterConfidence): "credit" | "pending" | "debit" {
  return c === "high" ? "credit" : c === "medium" ? "pending" : "debit";
}

export function confidenceLabel(c: ClusterConfidence): string {
  return c === "high" ? "HIGH" : c === "medium" ? "MED" : "LOW";
}

/** Pretty-print a flag list ("refund", "missing_data" -> "Refund · Missing data"). */
export function flagLabel(flag: string): string {
  const map: Record<string, string> = {
    refund: "Refund",
    dispute: "Dispute",
    missing_data: "Missing data",
    high_value: "High value",
    substantiation: "§274(d) needed",
    high_materiality: "High materiality"
  };
  return map[flag] ?? flag.replace(/_/g, " ");
}
