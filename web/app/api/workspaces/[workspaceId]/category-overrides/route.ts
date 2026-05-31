// Proxy: GET + POST /api/workspaces/{workspaceId}/category-overrides
//
// Called by:
//   - components/queue-row-actions.tsx (POST when user trains a new rule)
// Backend: app/api/category_overrides.py

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
  const search = req.nextUrl.searchParams.toString();
  const res = await fetch(
    `${INTERNAL_BASE}/api/workspaces/${workspaceId}/category-overrides${search ? `?${search}` : ""}`,
    {
      headers: { Authorization: `Bearer ${session.accessToken}` },
      cache: "no-store",
    }
  );
  const respBody = await res.text();
  return new NextResponse(respBody, {
    status: res.status,
    headers: { "Content-Type": "application/json" },
  });
}

export async function POST(
  req: NextRequest,
  ctx: { params: Promise<{ workspaceId: string }> }
) {
  const session = await auth();
  if (!session?.accessToken) {
    return NextResponse.json({ error: "Unauthorized" }, { status: 401 });
  }
  const { workspaceId } = await ctx.params;
  const body = await req.text();
  const res = await fetch(
    `${INTERNAL_BASE}/api/workspaces/${workspaceId}/category-overrides`,
    {
      method: "POST",
      headers: {
        Authorization: `Bearer ${session.accessToken}`,
        "Content-Type": "application/json",
      },
      body,
      cache: "no-store",
    }
  );
  const respBody = await res.text();
  return new NextResponse(respBody, {
    status: res.status,
    headers: { "Content-Type": "application/json" },
  });
}
