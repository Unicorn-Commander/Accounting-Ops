// Initiates the eBay OAuth handshake.
//
// The browser hits /api/connectors/ebay/start?workspace_id=...&entity_id=...
// from a logged-in Next.js session. We forward to the backend's
// /api/connectors/ebay/oauth/start with the user's Bearer token, follow the
// backend's 302 to eBay's auth URL, then re-emit that 302 to the browser so
// the user lands on eBay's consent page.
//
// We can't simply window.location.href to the backend URL because the browser
// won't carry the Bearer header on a top-level navigation — and we don't want
// to ship the access token as a query param.

import { NextRequest, NextResponse } from "next/server";
import { auth } from "@/lib/auth";

const INTERNAL_BASE =
  process.env.INTERNAL_API_BASE_URL ??
  process.env.NEXT_PUBLIC_API_BASE_URL ??
  "http://app:8000";

export async function GET(req: NextRequest) {
  const session = await auth();
  if (!session?.accessToken) {
    return NextResponse.redirect(new URL("/login", req.url));
  }
  const workspace_id = req.nextUrl.searchParams.get("workspace_id");
  const entity_id = req.nextUrl.searchParams.get("entity_id");
  if (!workspace_id || !entity_id) {
    return NextResponse.json(
      { error: "workspace_id and entity_id query params required" },
      { status: 400 }
    );
  }
  const backendUrl = new URL(`${INTERNAL_BASE}/api/connectors/ebay/oauth/start`);
  backendUrl.searchParams.set("workspace_id", workspace_id);
  backendUrl.searchParams.set("entity_id", entity_id);

  // Backend returns a 302 to eBay; we follow manually so we can re-emit it
  // (a built-in fetch redirect would consume the 302 silently).
  const resp = await fetch(backendUrl, {
    method: "GET",
    redirect: "manual",
    headers: {
      Authorization: `Bearer ${session.accessToken}`
    }
  });
  if (resp.status === 302 || resp.status === 307) {
    const location = resp.headers.get("location");
    if (!location) {
      return NextResponse.json(
        { error: "Backend 302 missing Location header" },
        { status: 502 }
      );
    }
    return NextResponse.redirect(location, 302);
  }
  // Surface the backend's error verbatim so the user sees what went wrong.
  const body = await resp.text();
  return new NextResponse(body, {
    status: resp.status,
    headers: { "content-type": resp.headers.get("content-type") || "text/plain" }
  });
}
