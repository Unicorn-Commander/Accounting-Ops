// Proxy: POST /api/workspaces/{workspaceId}/stripe/blocked-item/reopen
//
// Called by components/stripe-blocked-item-panel.tsx. Owner/admin in an
// interactive human session only; the backend enforces that
// (require_owner_or_admin_human) and answers 409 "retry shortly" while a sync
// pass holds the Stripe state lock. This proxy forwards the bearer and the
// body verbatim and never logs either.
//
// Backend: app/api/stripe_blocked_item.py

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
    `${INTERNAL_BASE}/api/workspaces/${encodeURIComponent(workspaceId)}/stripe/blocked-item/reopen`,
    {
      method: "POST",
      headers: {
        Authorization: `Bearer ${session.accessToken}`,
        "Content-Type": "application/json"
      },
      body,
      cache: "no-store"
    }
  );
  const respBody = await res.text();
  return new NextResponse(respBody, {
    status: res.status,
    headers: { "Content-Type": "application/json" }
  });
}
