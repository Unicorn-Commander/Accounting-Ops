// Proxy: POST /api/workspaces/{workspaceId}/journal-entries
//
// The browser never speaks to the FastAPI service directly because it doesn't
// have the user's Keycloak Bearer (NextAuth keeps that server-side). This
// thin proxy attaches the Bearer + forwards the body verbatim.
//
// Called by:
//   - components/queue-row-actions.tsx        (confirm + post)
//   - components/queue-column.tsx             (bulk post)
//   - components/bank-txn-import-row.tsx      (single-row post)
//   - app/journal/new/manual-entry-form.tsx   (manual entry)
//
// The backend route is in app/api/journal.py @router.post(
// "/api/workspaces/{workspace_id}/journal-entries").

import { NextRequest, NextResponse } from "next/server";
import { auth } from "@/lib/auth";

const INTERNAL_BASE =
  process.env.INTERNAL_API_BASE_URL ??
  process.env.NEXT_PUBLIC_API_BASE_URL ??
  "http://app:8000";

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
    `${INTERNAL_BASE}/api/workspaces/${workspaceId}/journal-entries`,
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
    `${INTERNAL_BASE}/api/workspaces/${workspaceId}/journal-entries${search ? `?${search}` : ""}`,
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
