import { NextRequest, NextResponse } from "next/server";
import { auth } from "@/lib/auth";

const INTERNAL_BASE =
  process.env.INTERNAL_API_BASE_URL ??
  process.env.NEXT_PUBLIC_API_BASE_URL ??
  "http://app:8000";

export async function POST(
  _req: NextRequest,
  ctx: { params: Promise<{ workspaceId: string }> }
) {
  const session = await auth();
  if (!session?.accessToken) {
    return NextResponse.json({ error: "Unauthorized" }, { status: 401 });
  }
  const { workspaceId } = await ctx.params;
  const res = await fetch(
    `${INTERNAL_BASE}/api/workspaces/${workspaceId}/sentinel/run`,
    {
      method: "POST",
      headers: { Authorization: `Bearer ${session.accessToken}` },
      cache: "no-store"
    }
  );
  const text = await res.text();
  return new NextResponse(text, {
    status: res.status,
    headers: { "Content-Type": "application/json" }
  });
}
