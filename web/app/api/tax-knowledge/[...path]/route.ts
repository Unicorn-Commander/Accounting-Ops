// Pass-through proxy: tax-knowledge browser endpoints.
//
// Browser → /api/tax-knowledge/topics
//         → app:8000/api/tax-knowledge/topics
//
// Browser → /api/tax-knowledge/documents?jurisdiction=US&topic=174
//         → app:8000/api/tax-knowledge/documents?...
//
// Browser → /api/tax-knowledge/documents/{id}
//         → app:8000/api/tax-knowledge/documents/{id}
//
// The proxy forwards the full path verbatim. Auth via the signed-in user's
// Keycloak access token (server-side fetch).

import { auth } from "@/lib/auth";
import { type NextRequest, NextResponse } from "next/server";

const INTERNAL_BASE =
  process.env.INTERNAL_API_BASE_URL ??
  process.env.NEXT_PUBLIC_API_BASE_URL ??
  "http://app:8000";

export async function GET(
  req: NextRequest,
  ctx: { params: Promise<{ path: string[] }> }
) {
  const session = await auth();
  if (!session?.accessToken) {
    return NextResponse.json({ error: "Unauthorized" }, { status: 401 });
  }

  const { path } = await ctx.params;
  const qs = req.nextUrl.searchParams.toString();
  const upstream =
    `${INTERNAL_BASE}/api/tax-knowledge/${path.join("/")}` +
    (qs ? `?${qs}` : "");

  let resp: Response;
  try {
    resp = await fetch(upstream, {
      headers: { Authorization: `Bearer ${session.accessToken}` },
      cache: "no-store",
    });
  } catch (err) {
    console.error("[tax-knowledge proxy] upstream fetch failed:", err);
    return NextResponse.json(
      { error: "upstream_unreachable" },
      { status: 502 }
    );
  }

  const body = await resp.text();
  return new NextResponse(body, {
    status: resp.status,
    headers: {
      "content-type":
        resp.headers.get("content-type") ?? "application/json",
    },
  });
}
