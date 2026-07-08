// Proxy: POST /api/workspaces/{workspaceId}/connectors/stripe/key
//
// Called by components/connect-stripe-modal.tsx when an admin pastes a Stripe
// restricted API key. The backend validates it against Stripe's /v1/account
// endpoint, encrypts it, and stores in tenant_credentials. The plaintext key
// NEVER comes back from the backend — only sanitized metadata.
//
// Backend: app/api/stripe_oauth.py — stripe_store_key

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
    `${INTERNAL_BASE}/api/workspaces/${workspaceId}/connectors/stripe/key`,
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
