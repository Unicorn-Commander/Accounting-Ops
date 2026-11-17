// Client-safe calls + types for the blocked Stripe item panel.
//
// Backend: app/api/stripe_blocked_item.py
//   GET  /api/workspaces/{wid}/stripe/blocked-item
//   POST /api/workspaces/{wid}/stripe/blocked-item/resolve
//        {bt_id, entity_id?, journal_entry_id?, not_books_relevant_reason?}
//        exactly one of journal_entry_id / not_books_relevant_reason
//   POST /api/workspaces/{wid}/stripe/blocked-item/reopen  {bt_id, entity_id?, reason}
// Reasons are MIN_REASON_CHARS to MAX_REASON_CHARS after trimming
// (app/connectors/stripe/blocked_resolution.py _clean_reason).

import { postJson, type ApiFailure, type FetchLike } from "@/lib/api-error";

export const MIN_REASON_CHARS = 10;
export const MAX_REASON_CHARS = 2000;

export type BlockedStripeItem = {
  bt_id: string;
  entity_id: string;
  entity_display_name: string | null;
  amount: number | null;
  currency: string | null;
  error: string | null;
  failure_count: number;
  since: string | null;
  approval_id: string | null;
  approval_status: string | null;
};

export type ResolvedStripeItem = {
  bt_id: string;
  entity_id: string;
  entity_display_name: string | null;
  kind: "journal_entry" | "not_books_relevant" | string | null;
  resolved_by_uc_uid: string | null;
  resolved_at: string | null;
  journal_entry_id: string | null;
  not_books_relevant_reason: string | null;
  amount: number | null;
  currency: string | null;
  error: string | null;
  approval_id: string | null;
  audit_id: string | null;
};

export type BlockedItemView = {
  blocked: BlockedStripeItem[];
  resolved: ResolvedStripeItem[];
  min_reason_chars: number;
  max_reason_chars: number;
};

export function blockedItemUrl(workspaceId: string, action?: "resolve" | "reopen"): string {
  const base = `/api/workspaces/${encodeURIComponent(workspaceId)}/stripe/blocked-item`;
  return action ? `${base}/${action}` : base;
}

export function reasonLength(reason: string): number {
  return reason.trim().length;
}

export function reasonProblem(reason: string, min = MIN_REASON_CHARS, max = MAX_REASON_CHARS): string | null {
  const n = reasonLength(reason);
  if (n < min) return `At least ${min} characters (${n} so far).`;
  if (n > max) return `At most ${max} characters (${n} now).`;
  return null;
}

export type ResolveInput =
  | { mode: "journal_entry"; btId: string; entityId: string | null; journalEntryId: string }
  | { mode: "not_books_relevant"; btId: string; entityId: string | null; reason: string };

export type ResolveBody = {
  bt_id: string;
  entity_id?: string;
  journal_entry_id?: string;
  not_books_relevant_reason?: string;
};

/** The exact resolve body: one justification, never both. */
export function buildResolveBody(input: ResolveInput): ResolveBody {
  const body: ResolveBody = { bt_id: input.btId };
  if (input.entityId) body.entity_id = input.entityId;
  if (input.mode === "journal_entry") body.journal_entry_id = input.journalEntryId;
  else body.not_books_relevant_reason = input.reason.trim();
  return body;
}

export type ReopenBody = { bt_id: string; entity_id?: string; reason: string };

export function buildReopenBody(btId: string, entityId: string | null, reason: string): ReopenBody {
  const body: ReopenBody = { bt_id: btId, reason: reason.trim() };
  if (entityId) body.entity_id = entityId;
  return body;
}

export async function resolveBlockedItem(
  fetchImpl: FetchLike,
  workspaceId: string,
  input: ResolveInput,
): Promise<{ ok: true; data: Record<string, unknown> } | ApiFailure> {
  return postJson(fetchImpl, blockedItemUrl(workspaceId, "resolve"), buildResolveBody(input));
}

export async function reopenResolvedItem(
  fetchImpl: FetchLike,
  workspaceId: string,
  btId: string,
  entityId: string | null,
  reason: string,
): Promise<{ ok: true; data: { warning?: string } & Record<string, unknown> } | ApiFailure> {
  return postJson(fetchImpl, blockedItemUrl(workspaceId, "reopen"), buildReopenBody(btId, entityId, reason));
}

export async function fetchBlockedItemView(fetchImpl: FetchLike, workspaceId: string): Promise<BlockedItemView | null> {
  try {
    const res = await fetchImpl(blockedItemUrl(workspaceId), { headers: { Accept: "application/json" }, cache: "no-store" });
    if (!res.ok) return null;
    return (await res.json()) as BlockedItemView;
  } catch {
    return null;
  }
}

/** Owner/admin only, mirroring require_owner_or_admin_human. */
export function canManageBlockedItem(role: string | null | undefined): boolean {
  return role === "owner" || role === "admin";
}
