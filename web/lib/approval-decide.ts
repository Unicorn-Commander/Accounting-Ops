// Client-safe call to the existing decide proxy
// (web/app/api/workspaces/[workspaceId]/approvals/[approvalId]/decide).

import { postJson, type ApiFailure, type FetchLike } from "@/lib/api-error";

export type DecideResponse = {
  ok?: boolean;
  decision?: "approved" | "rejected";
  applied?: boolean;
  detail?: string;
  reversal_entry_id?: string;
  restated_entry_id?: string;
  rule_id?: string;
  active_profile_id?: string;
};

export function decideUrl(workspaceId: string, approvalId: string): string {
  return `/api/workspaces/${encodeURIComponent(workspaceId)}/approvals/${encodeURIComponent(approvalId)}/decide`;
}

export async function decideApproval(
  fetchImpl: FetchLike,
  workspaceId: string,
  approvalId: string,
  decision: "approve" | "reject",
): Promise<{ ok: true; data: DecideResponse } | ApiFailure> {
  // Body is exactly the backend DecideRequest: no note, no category override.
  return postJson<DecideResponse>(fetchImpl, decideUrl(workspaceId, approvalId), { decision });
}
