// Audit 2026-05-31 Bug 15: this Next.js proxy was historically described as
// "dead-code because Traefik shadows /api/*". That was true BEFORE the
// 2026-05-30 Traefik flip (commit f1a92be) which routes /api/* to web by
// default. After the flip, web is the front door and Traefik's catch-all
// covers /api/workspaces/*. This specific /api/coa/[workspaceId]/[entityId]
// route stays as a compatibility proxy for any client that still hits it
// directly (the SSR-rendered CoaEditor uses router.refresh() rather than
// fetching this route on entity-switch, but the route itself is live).
import { NextRequest, NextResponse } from "next/server";
import { auth } from "@/lib/auth";

const INTERNAL_BASE =
  process.env.INTERNAL_API_BASE_URL ??
  process.env.NEXT_PUBLIC_API_BASE_URL ??
  "http://app:8000";

export async function GET(
  _req: NextRequest,
  ctx: { params: Promise<{ workspaceId: string; entityId: string }> }
) {
  const session = await auth();
  if (!session?.accessToken) {
    return NextResponse.json({ error: "Unauthorized" }, { status: 401 });
  }
  const { workspaceId, entityId } = await ctx.params;
  const res = await fetch(
    `${INTERNAL_BASE}/api/workspaces/${workspaceId}/coa?entity_id=${entityId}`,
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

export async function POST(
  req: NextRequest,
  ctx: { params: Promise<{ workspaceId: string; entityId: string }> }
) {
  const session = await auth();
  if (!session?.accessToken) {
    return NextResponse.json({ error: "Unauthorized" }, { status: 401 });
  }
  const { workspaceId, entityId } = await ctx.params;
  const body = await req.json();
  const res = await fetch(
    `${INTERNAL_BASE}/api/workspaces/${workspaceId}/coa`,
    {
      method: "POST",
      headers: {
        Authorization: `Bearer ${session.accessToken}`,
        "Content-Type": "application/json"
      },
      body: JSON.stringify({ ...body, entity_id: entityId })
    }
  );
  const respBody = await res.text();
  return new NextResponse(respBody, {
    status: res.status,
    headers: { "Content-Type": "application/json" }
  });
}

export async function PATCH(
  req: NextRequest,
  ctx: { params: Promise<{ workspaceId: string; entityId: string }> }
) {
  const session = await auth();
  if (!session?.accessToken) {
    return NextResponse.json({ error: "Unauthorized" }, { status: 401 });
  }
  const { workspaceId } = await ctx.params;
  const accountId = req.nextUrl.searchParams.get("accountId");
  if (!accountId) {
    return NextResponse.json({ error: "Missing accountId query param" }, { status: 400 });
  }
  const body = await req.json();
  const res = await fetch(
    `${INTERNAL_BASE}/api/workspaces/${workspaceId}/coa/${accountId}`,
    {
      method: "PATCH",
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

export async function DELETE(
  req: NextRequest,
  ctx: { params: Promise<{ workspaceId: string; entityId: string }> }
) {
  const session = await auth();
  if (!session?.accessToken) {
    return NextResponse.json({ error: "Unauthorized" }, { status: 401 });
  }
  const { workspaceId } = await ctx.params;
  const accountId = req.nextUrl.searchParams.get("accountId");
  if (!accountId) {
    return NextResponse.json({ error: "Missing accountId query param" }, { status: 400 });
  }
  const res = await fetch(
    `${INTERNAL_BASE}/api/workspaces/${workspaceId}/coa/${accountId}`,
    {
      method: "DELETE",
      headers: { Authorization: `Bearer ${session.accessToken}` }
    }
  );
  const respBody = await res.text();
  return new NextResponse(respBody, {
    status: res.status,
    headers: { "Content-Type": "application/json" }
  });
}
