// Proxy: POST /api/workspaces/{workspaceId}/connectors/stripe/webhook-secret
//
// Called by the "Set webhook secret" button on the Stripe status card.
// Aaron pastes the whsec_... value Stripe gave him after creating the
// webhook endpoint at dashboard.stripe.com/webhooks. The backend merges
// it into the existing encrypted credential blob.
//
// Backend: app/api/stripe_oauth.py — stripe_set_webhook_secret

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
    `${INTERNAL_BASE}/api/workspaces/${workspaceId}/connectors/stripe/webhook-secret`,
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
