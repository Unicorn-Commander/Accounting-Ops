import { NextResponse } from "next/server";
import { auth } from "@/lib/auth";

const INTERNAL_BASE =
  process.env.INTERNAL_API_BASE_URL ??
  process.env.NEXT_PUBLIC_API_BASE_URL ??
  "http://app:8000";

export async function POST(
  req: Request,
  ctx: { params: Promise<{ workspaceId: string; entityId: string }> }
) {
  const session = await auth();
  if (!session?.accessToken) {
    return NextResponse.json({ detail: "Unauthorized" }, { status: 401 });
  }
  const { workspaceId, entityId } = await ctx.params;
  const body = await req.json().catch(() => ({}));
  const res = await fetch(
    `${INTERNAL_BASE}/api/workspaces/${workspaceId}/entities/${entityId}/close-period`,
    {
      method: "POST",
      headers: {
        Authorization: `Bearer ${session.accessToken}`,
        Accept: "application/json",
        "Content-Type": "application/json",
      },
      body: JSON.stringify(body),
      cache: "no-store",
    }
  );
  const data = await res.json().catch(() => ({ detail: res.statusText }));
  return NextResponse.json(data, { status: res.status });
}
