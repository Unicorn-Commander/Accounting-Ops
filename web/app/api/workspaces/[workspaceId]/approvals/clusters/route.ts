import { NextRequest, NextResponse } from "next/server";
import { auth } from "@/lib/auth";

const INTERNAL_BASE =
  process.env.INTERNAL_API_BASE_URL ??
  process.env.NEXT_PUBLIC_API_BASE_URL ??
  "http://app:8000";

/**
 * GET /api/workspaces/{wid}/approvals/clusters?entity_id=...
 *
 * Server-side proxy to the FastAPI cluster aggregation endpoint. Used by
 * the cluster-card UX in /approvals (the page component fetches this
 * server-side via lib/approval-clusters.ts; this proxy exists for any
 * client-side re-fetch we add later).
 */
export async function GET(
  req: NextRequest,
  ctx: { params: Promise<{ workspaceId: string }> }
) {
  const session = await auth();
  if (!session?.accessToken) {
    return NextResponse.json({ error: "Unauthorized" }, { status: 401 });
  }
  const { workspaceId } = await ctx.params;
  const url = new URL(req.url);
  const params = new URLSearchParams();
  const entityId = url.searchParams.get("entity_id");
  if (entityId) params.set("entity_id", entityId);
  const res = await fetch(
    `${INTERNAL_BASE}/api/workspaces/${workspaceId}/approvals/clusters?${params}`,
    {
      method: "GET",
      headers: {
        Authorization: `Bearer ${session.accessToken}`,
        Accept: "application/json"
      },
      cache: "no-store"
    }
  );
  const respBody = await res.text();
  return new NextResponse(respBody, {
    status: res.status,
    headers: { "Content-Type": "application/json" }
  });
}
