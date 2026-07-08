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
  flags: string[];
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
  items: ApprovalItem[];
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
