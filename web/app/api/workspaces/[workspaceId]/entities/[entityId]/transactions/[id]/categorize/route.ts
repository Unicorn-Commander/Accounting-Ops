import { NextRequest, NextResponse } from "next/server";
import { auth } from "@/lib/auth";

const INTERNAL_BASE =
  process.env.INTERNAL_API_BASE_URL ??
  process.env.NEXT_PUBLIC_API_BASE_URL ??
  "http://app:8000";

// Proxies to the entity-scoped backend route (app/api/tools.py):
//   POST /api/workspaces/{workspace_id}/entities/{entity_id}/transactions/{id}/categorize
// Role floor is enforced server-side (bookkeeper) — this route just forwards
// the bearer token and body. Mirrors the close-period proxy pattern.
export async function POST(
  req: NextRequest,
  ctx: { params: Promise<{ workspaceId: string; entityId: string; id: string }> }
) {
  const session = await auth();
  if (!session?.accessToken) {
    return NextResponse.json({ detail: "Unauthorized" }, { status: 401 });
  }
  const { workspaceId, entityId, id } = await ctx.params;
  const body = await req.json().catch(() => ({}));
  const res = await fetch(
    `${INTERNAL_BASE}/api/workspaces/${workspaceId}/entities/${entityId}/transactions/${id}/categorize`,
    {
      method: "POST",
      headers: {
        Authorization: `Bearer ${session.accessToken}`,
        Accept: "application/json",
        "Content-Type": "application/json"
      },
      body: JSON.stringify(body),
      cache: "no-store"
    }
  );
  const respBody = await res.text();
  return new NextResponse(respBody, {
    status: res.status,
    headers: { "Content-Type": "application/json" }
  });
}
