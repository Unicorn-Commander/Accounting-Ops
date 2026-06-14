// Proxy: GET /api/workspaces/{workspaceId}/cfo-brief
// Client-side fetch path for the ⌘K palette mini-cards. Adds the user's
// Keycloak bearer + forwards query params to the FastAPI cfo-brief assembler.

import { NextRequest, NextResponse } from "next/server";
import { auth } from "@/lib/auth";

const INTERNAL_BASE =
  process.env.INTERNAL_API_BASE_URL ??
  process.env.NEXT_PUBLIC_API_BASE_URL ??
  "http://app:8000";

export async function GET(
  req: NextRequest,
  ctx: { params: Promise<{ workspaceId: string }> }
) {
  const session = await auth();
  if (!session?.accessToken) {
    return NextResponse.json({ error: "Unauthorized" }, { status: 401 });
  }
  const { workspaceId } = await ctx.params;
  const qs = req.nextUrl.searchParams.toString();
  const res = await fetch(
    `${INTERNAL_BASE}/api/workspaces/${workspaceId}/cfo-brief${qs ? "?" + qs : ""}`,
    {
      headers: {
        Authorization: `Bearer ${session.accessToken}`,
        Accept: "application/json"
      },
      cache: "no-store"
    }
  );
  const body = await res.text();
  return new NextResponse(body, {
    status: res.status,
    headers: { "Content-Type": "application/json" }
  });
}
