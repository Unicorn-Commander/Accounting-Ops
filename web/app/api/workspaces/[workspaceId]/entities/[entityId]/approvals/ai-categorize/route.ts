import { NextRequest, NextResponse } from "next/server";
import { auth } from "@/lib/auth";

const INTERNAL_BASE =
  process.env.INTERNAL_API_BASE_URL ??
  process.env.NEXT_PUBLIC_API_BASE_URL ??
  "http://app:8000";

// Proxies to the entity-scoped backend route (app/api/tools.py):
//   POST /api/workspaces/{workspace_id}/entities/{entity_id}/approvals/ai-categorize
//     ?limit=&year=&dry_run=
// limit/year/dry_run are query params on the backend route (no request body),
// so this proxy just forwards the incoming query string. Role floor
// (bookkeeper) is enforced server-side. Mirrors the close-period proxy
// pattern used for other entity-scoped writes.
export async function POST(
  req: NextRequest,
  ctx: { params: Promise<{ workspaceId: string; entityId: string }> }
) {
  const session = await auth();
  if (!session?.accessToken) {
    return NextResponse.json({ detail: "Unauthorized" }, { status: 401 });
  }
  const { workspaceId, entityId } = await ctx.params;
  const qs = req.nextUrl.search; // includes leading "?" or ""
  const res = await fetch(
    `${INTERNAL_BASE}/api/workspaces/${workspaceId}/entities/${entityId}/approvals/ai-categorize${qs}`,
    {
      method: "POST",
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
