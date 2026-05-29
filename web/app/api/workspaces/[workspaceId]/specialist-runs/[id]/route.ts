import { NextRequest, NextResponse } from "next/server";
import { auth } from "@/lib/auth";

const INTERNAL_BASE =
  process.env.INTERNAL_API_BASE_URL ??
  process.env.NEXT_PUBLIC_API_BASE_URL ??
  "http://app:8000";

// GET   /api/workspaces/[workspaceId]/specialist-runs/[id] — single run (viewer)
// PATCH /api/workspaces/[workspaceId]/specialist-runs/[id] — finish a run (accountant)
//   Note: the /finish sub-path is handled here via PATCH for Next.js route simplicity;
//   the actual backend route is PATCH .../specialist-runs/{id}/finish.
//   Runner scripts should call the backend URL directly (they don't go through the web proxy).

export async function GET(
  _req: NextRequest,
  ctx: { params: Promise<{ workspaceId: string; id: string }> }
) {
  const session = await auth();
  if (!session?.accessToken) {
    return NextResponse.json({ error: "Unauthorized" }, { status: 401 });
  }
  const { workspaceId, id } = await ctx.params;
  const res = await fetch(
    `${INTERNAL_BASE}/api/workspaces/${workspaceId}/specialist-runs/${id}`,
    {
      headers: { Authorization: `Bearer ${session.accessToken}` },
      cache: "no-store"
    }
  );
  const body = await res.text();
  return new NextResponse(body, {
    status: res.status,
    headers: { "Content-Type": "application/json" }
  });
}
