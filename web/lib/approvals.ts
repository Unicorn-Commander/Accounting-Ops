// Server-side client for the approval-queue endpoints.
//
// Mirrors lib/journal.ts: a no-store fetch to the FastAPI backend using the
// signed-in user's Keycloak access token. The backend enforces RBAC
// (accountant+ to list, admin to decide) + entity-scope — the web layer only
// passes the token through.

import { auth } from "@/lib/auth";

const INTERNAL_BASE =
  process.env.INTERNAL_API_BASE_URL ??
  process.env.NEXT_PUBLIC_API_BASE_URL ??
  "http://app:8000";

export type ApprovalSubjectType =
  | "journal_recategorization"
  | "compliance_rule"
  | "bank_transaction_import";

export type ApprovalPayload = {
  action?: string;
  // recat
  original_entry_id?: string;
  from_category?: string;
  new_category?: string;
  amount_cents?: number;
  agent_reasoning?: string;
  materiality_tier?: string;
  // rule
  rule_id?: string;
  pending_profile_id?: string;
  kind?: string;
  trigger?: string;
  action_enum?: string;
  // bank_transaction_import
  source_external_id?: string;
  effective_date?: string;
  description?: string;
  payee?: string;
  category?: string;
  category_status?: string;
  categorization_source?: string;
  debit_account_hint?: string;
  credit_account_hint?: string;
  block_reason?: string;
  llm_suggested?: boolean;
};

export type Approval = {
  id: string;
  entity_id: string;
  entity_display_name: string | null;
  entity_slug: string | null;
  subject_type: ApprovalSubjectType | string;
  subject_id: string;
  amount: string | null;
  currency_code: string | null;
  status: "pending" | "approved" | "rejected" | "canceled";
  reasoning: string;
  requested_by_identity: string;
  decided_by_uc_uid: string | null;
  decided_at: string | null;
  created_at: string;
  payload: ApprovalPayload | null;
};

export type ApprovalListResponse = {
  approvals: Approval[];
  total: number;
};

export async function getApprovals(
  workspaceId: string,
  opts: { entityId?: string; status?: string } = {}
): Promise<ApprovalListResponse | null> {
  const session = await auth();
  if (!session?.accessToken) return null;
  const params = new URLSearchParams();
  if (opts.entityId) params.set("entity_id", opts.entityId);
  params.set("status_filter", opts.status ?? "pending");
  try {
    const res = await fetch(
      `${INTERNAL_BASE}/api/workspaces/${workspaceId}/approvals?${params}`,
      {
        headers: {
          Authorization: `Bearer ${session.accessToken}`,
          Accept: "application/json"
        },
        cache: "no-store"
      }
    );
    if (!res.ok) return null;
    return (await res.json()) as ApprovalListResponse;
  } catch {
    return null;
  }
}

export async function getApprovalCategories(
  workspaceId: string
): Promise<string[]> {
  const session = await auth();
  if (!session?.accessToken) return [];
  try {
    const res = await fetch(
      `${INTERNAL_BASE}/api/workspaces/${workspaceId}/approvals/categories`,
      {
        headers: {
          Authorization: `Bearer ${session.accessToken}`,
          Accept: "application/json"
        },
        cache: "no-store"
      }
    );
    if (!res.ok) return [];
    const data = (await res.json()) as { categories: string[] };
    return data.categories;
  } catch {
    return [];
  }
}
