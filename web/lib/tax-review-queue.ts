// Server-side client for the tax-review-queue endpoints.
//
// Mirrors lib/approvals.ts: a no-store fetch to the FastAPI backend using the
// signed-in user's Keycloak access token. The backend enforces RBAC
// (accountant+ to list, accountant floor to resolve). The web layer only
// passes the token through.

import { auth } from "@/lib/auth";

const INTERNAL_BASE =
  process.env.INTERNAL_API_BASE_URL ??
  process.env.NEXT_PUBLIC_API_BASE_URL ??
  "http://app:8000";

// ---------------------------------------------------------------------------
// Types — mirror the backend TaxReviewItem schema
// ---------------------------------------------------------------------------

export type TaxReviewItemType =
  | "form_line_proposal"
  | "flagged_position"
  | "memo"
  | "question";

export type TaxReviewStatus =
  | "pending"
  | "reviewed"
  | "resolved"
  | "rejected";

export type TaxReviewConfidence = "low" | "medium" | "high";

export type TaxReviewCitation = {
  type: "section" | "journal_entry" | "prior_year" | "irs_pub";
  value: string;
};

// content shape varies by item_type; keep as `unknown` and narrow at render
// eslint-disable-next-line @typescript-eslint/no-explicit-any
export type TaxReviewItemContent = any;

export type TaxReviewItem = {
  id: string;
  entity_id: string;
  tax_year: number;
  item_type: TaxReviewItemType;
  form_reference: string | null;
  content: TaxReviewItemContent;
  confidence: TaxReviewConfidence;
  citations: TaxReviewCitation[] | null;
  status: TaxReviewStatus;
  submitter_kind: "agent" | "human";
  submitter_id: string;
  reviewer_uc_uid: string | null;
  resolution_notes: string | null;
  created_at: string;
  resolved_at: string | null;
};

export type TaxReviewQueueResponse = {
  items: TaxReviewItem[];
  total: number;
};

export type ResolvePayload = {
  status: "reviewed" | "resolved" | "rejected";
  resolution_notes?: string;
};

// ---------------------------------------------------------------------------
// Server-side fetchers — call the FastAPI backend directly with user token.
// Use these in Server Components / RSC pages.
// ---------------------------------------------------------------------------

export async function fetchTaxReviewQueue(
  workspaceId: string,
  entityId: string,
  taxYear: number,
  opts: { status?: string; item_type?: string } = {}
): Promise<TaxReviewQueueResponse> {
  const session = await auth();
  if (!session?.accessToken) {
    return { items: [], total: 0 };
  }
  const params = new URLSearchParams({
    entity_id: entityId,
    tax_year: String(taxYear)
  });
  if (opts.status) params.set("status", opts.status);
  if (opts.item_type) params.set("item_type", opts.item_type);

  try {
    const res = await fetch(
      `${INTERNAL_BASE}/api/workspaces/${workspaceId}/tax-review-queue?${params}`,
      {
        headers: {
          Authorization: `Bearer ${session.accessToken}`,
          Accept: "application/json"
        },
        cache: "no-store"
      }
    );
    if (!res.ok) return { items: [], total: 0 };
    return (await res.json()) as TaxReviewQueueResponse;
  } catch {
    return { items: [], total: 0 };
  }
}

export async function fetchTaxReviewItem(
  workspaceId: string,
  id: string
): Promise<TaxReviewItem | null> {
  const session = await auth();
  if (!session?.accessToken) return null;

  try {
    const res = await fetch(
      `${INTERNAL_BASE}/api/workspaces/${workspaceId}/tax-review-queue/${id}`,
      {
        headers: {
          Authorization: `Bearer ${session.accessToken}`,
          Accept: "application/json"
        },
        cache: "no-store"
      }
    );
    if (!res.ok) return null;
    return (await res.json()) as TaxReviewItem;
  } catch {
    return null;
  }
}
