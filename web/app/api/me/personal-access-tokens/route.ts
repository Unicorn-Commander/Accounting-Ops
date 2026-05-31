// Proxy: list (GET) + mint (POST) personal access tokens.
// The signed-in user's Keycloak bearer is forwarded to the FastAPI backend.

import { NextRequest, NextResponse } from "next/server";
import { auth } from "@/lib/auth";

const INTERNAL_BASE =
  process.env.INTERNAL_API_BASE_URL ??
  process.env.NEXT_PUBLIC_API_BASE_URL ??
  "http://app:8000";

export async function GET() {
  const session = await auth();
  if (!session?.accessToken) {
    return NextResponse.json({ error: "Unauthorized" }, { status: 401 });
  }
  const res = await fetch(`${INTERNAL_BASE}/api/me/personal-access-tokens`, {
    headers: { Authorization: `Bearer ${session.accessToken}` },
    cache: "no-store"
  });
  const body = await res.text();
  return new NextResponse(body, {
    status: res.status,
    headers: { "Content-Type": "application/json" }
  });
}

export async function POST(req: NextRequest) {
  const session = await auth();
  if (!session?.accessToken) {
    return NextResponse.json({ error: "Unauthorized" }, { status: 401 });
  }
  const body = await req.text();
  const res = await fetch(`${INTERNAL_BASE}/api/me/personal-access-tokens`, {
    method: "POST",
    headers: {
      Authorization: `Bearer ${session.accessToken}`,
      "Content-Type": "application/json"
    },
    body,
    cache: "no-store"
  });
  const out = await res.text();
  return new NextResponse(out, {
    status: res.status,
    headers: { "Content-Type": "application/json" }
  });
}
