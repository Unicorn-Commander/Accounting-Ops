// Types and fetchers for the approval-recommendations API.
//
// The backend endpoint is:
//   GET /api/workspaces/{wid}/approval-recommendations
//       ?entity_id=&cluster_id=&status=pending&limit=&offset=
//
// This mirrors lib/approvals.ts: a no-store server-side fetch using the
// signed-in user's Keycloak access token.

import { auth } from "@/lib/auth";

const INTERNAL_BASE =
  process.env.INTERNAL_API_BASE_URL ??
  process.env.NEXT_PUBLIC_API_BASE_URL ??
  "http://app:8000";

// -----------------------------------------------------------------------
// Types
// -----------------------------------------------------------------------

export type RecommendationDecision = "approve" | "flag_for_review" | "reject";
export type RecommendationConfidence = "high" | "medium" | "low";

export type ApprovalRecommendation = {
  recommendation_id: string;
  approval_id: string;
  cluster_id: string;
  recommended_decision: RecommendationDecision;
  recommended_category: string | null;
  confidence: RecommendationConfidence;
  rationale: string;
  submitter_kind: string | null;
  submitter_id: string | null;
  created_at: string;
  // Target approval fields (joined)
  entity_id: string;
  approval_status: "pending" | "approved" | "rejected" | "canceled" | "skipped";
  amount: string | null;
  currency_code: string | null;
  payload: Record<string, unknown> | null;
  approval_created_at: string;
};

export type RecommendationCluster = {
  cluster_id: string;
  count: number;
  confidence: RecommendationConfidence;
  recommended_decision: RecommendationDecision;
  has_category_count: number;
  representative_category: string | null;
  // Client-side computed (from the raw recommendations list)
  total_amount_cents?: number;
  sample_payees?: string[];
};

export type RecommendationListResponse = {
  recommendations: ApprovalRecommendation[];
  total: number;
  clusters: RecommendationCluster[];
};

// -----------------------------------------------------------------------
// Fetcher
// -----------------------------------------------------------------------

export async function getApprovalRecommendations(
  workspaceId: string,
  opts: {
    entityId?: string;
    clusterId?: string;
    status?: string;
    limit?: number;
    offset?: number;
  } = {}
): Promise<RecommendationListResponse | null> {
  const session = await auth();
  if (!session?.accessToken) return null;

  const params = new URLSearchParams();
  if (opts.entityId) params.set("entity_id", opts.entityId);
  if (opts.clusterId) params.set("cluster_id", opts.clusterId);
  params.set("status", opts.status ?? "pending");
  if (opts.limit != null) params.set("limit", String(opts.limit));
  if (opts.offset != null) params.set("offset", String(opts.offset));

  try {
    const res = await fetch(
      `${INTERNAL_BASE}/api/workspaces/${workspaceId}/approval-recommendations?${params}`,
      {
        headers: {
          Authorization: `Bearer ${session.accessToken}`,
          Accept: "application/json"
        },
        cache: "no-store"
      }
    );
    if (!res.ok) return null;
    return (await res.json()) as RecommendationListResponse;
  } catch {
    return null;
  }
}

// -----------------------------------------------------------------------
// Helpers
// -----------------------------------------------------------------------

/** Confidence label with color class for the badge. */
export function confidenceDisplay(c: RecommendationConfidence): {
  label: string;
  tone: "credit" | "pending" | "debit";
} {
  if (c === "high") return { label: "HIGH", tone: "credit" };
  if (c === "medium") return { label: "MED", tone: "pending" };
  return { label: "LOW", tone: "debit" };
}

/** Aggregate total_amount_cents for a cluster from the full recommendations list. */
export function clusterTotalCents(
  clusterId: string,
  recommendations: ApprovalRecommendation[]
): number {
  return recommendations
    .filter((r) => r.cluster_id === clusterId)
    .reduce((sum, r) => {
      const cents = Number((r.payload as { amount_cents?: number } | null)?.amount_cents ?? 0);
      return sum + Math.abs(cents);
    }, 0);
}

/** Extract all approval_ids for a given cluster from the recommendations list. */
export function clusterApprovalIds(
  clusterId: string,
  recommendations: ApprovalRecommendation[]
): string[] {
  return recommendations
    .filter((r) => r.cluster_id === clusterId && r.approval_status === "pending")
    .map((r) => r.approval_id);
}
