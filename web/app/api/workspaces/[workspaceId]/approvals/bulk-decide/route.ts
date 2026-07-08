import { NextRequest, NextResponse } from "next/server";
import { auth } from "@/lib/auth";

const INTERNAL_BASE =
  process.env.INTERNAL_API_BASE_URL ??
  process.env.NEXT_PUBLIC_API_BASE_URL ??
  "http://app:8000";

/**
 * POST /api/workspaces/{wid}/approvals/bulk-decide
 *
 * Server-side proxy to the FastAPI bulk-decide endpoint. Body shape:
 *   { cluster_id?, approval_ids?, decision, override_category?, note? }
 *
 * The backend caps at 200 items per call; the cluster-card UX chunks
 * larger groups client-side.
 */
export async function POST(
  req: NextRequest,
  ctx: { params: Promise<{ workspaceId: string }> }
) {
  const session = await auth();
  if (!session?.accessToken) {
    return NextResponse.json({ error: "Unauthorized" }, { status: 401 });
  }
  const { workspaceId } = await ctx.params;
  const body = await req.json();
  const res = await fetch(
    `${INTERNAL_BASE}/api/workspaces/${workspaceId}/approvals/bulk-decide`,
    {
      method: "POST",
      headers: {
        Authorization: `Bearer ${session.accessToken}`,
        "Content-Type": "application/json"
      },
      body: JSON.stringify(body)
    }
  );
  const respBody = await res.text();
  return new NextResponse(respBody, {
    status: res.status,
    headers: { "Content-Type": "application/json" }
  });
}
