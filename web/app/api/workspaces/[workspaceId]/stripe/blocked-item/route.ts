// Proxy: GET /api/workspaces/{workspaceId}/stripe/blocked-item
//
// Read-only view of the current blocked Stripe item and the manual
// resolutions, used by components/stripe-blocked-item-panel.tsx to refresh
// after a resolve or reopen. Forwards the bearer; never logs bodies.
//
// Backend: app/api/stripe_blocked_item.py get_stripe_blocked_item

import { NextRequest, NextResponse } from "next/server";
import { auth } from "@/lib/auth";

const INTERNAL_BASE =
  process.env.INTERNAL_API_BASE_URL ??
  process.env.NEXT_PUBLIC_API_BASE_URL ??
  "http://app:8000";

export async function GET(
  _req: NextRequest,
  ctx: { params: Promise<{ workspaceId: string }> }
) {
  const session = await auth();
  if (!session?.accessToken) {
    return NextResponse.json({ error: "Unauthorized" }, { status: 401 });
  }
  const { workspaceId } = await ctx.params;
  const res = await fetch(
    `${INTERNAL_BASE}/api/workspaces/${encodeURIComponent(workspaceId)}/stripe/blocked-item`,
    {
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
