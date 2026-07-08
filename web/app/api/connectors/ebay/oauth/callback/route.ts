// Completes the eBay OAuth handshake.
//
// eBay redirects the user's browser here (a plain top-level navigation, with
// NO Bearer token) after they grant consent on eBay's site. We forward the
// `code`+`state` to the FastAPI callback, which verifies the signed state
// token, exchanges the code for tokens, stores the encrypted credential, and
// returns a 302 to /connectors?ebay=connected. We relay that 302 to the browser.
//
// Why this passthrough exists: Traefik routes all /api/* to this Next.js tier,
// so eBay's redirect lands here, not on FastAPI. Without this handler the
// request 404s (or is 401'd by middleware), and the OAuth code is dropped —
// consent succeeds but no credential is ever stored. This mirrors the
// /api/connectors/ebay/start route on the inbound side.
//
// This route is intentionally PUBLIC (see web/middleware.ts): eBay's
// third-party redirect can't carry our app's auth, and the security lives in
// the HMAC-signed, single-use state token (see app/api/ebay_oauth.py). We
// forward only the OAuth query params and never read cookies/session here.

import { NextRequest, NextResponse } from "next/server";

const INTERNAL_BASE =
  process.env.INTERNAL_API_BASE_URL ??
  process.env.NEXT_PUBLIC_API_BASE_URL ??
  "http://app:8000";

export async function GET(req: NextRequest) {
  const backendUrl = new URL(
    `${INTERNAL_BASE}/api/connectors/ebay/oauth/callback`
  );
  // Pass through exactly the params eBay appends: code+state on success,
  // error+error_description on denial. Nothing else is forwarded.
  for (const key of ["code", "state", "error", "error_description"]) {
    const v = req.nextUrl.searchParams.get(key);
    if (v !== null) backendUrl.searchParams.set(key, v);
  }

  // The backend returns a 302 to /connectors?ebay=...; follow manually so we
  // can re-emit it (a built-in fetch redirect would consume the 302 silently).
  const resp = await fetch(backendUrl, { method: "GET", redirect: "manual" });

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
  // Surface the backend's error verbatim (e.g. 400 missing code/state, or a
  // token-exchange failure) rather than showing the user a blank page.
  const body = await resp.text();
  return new NextResponse(body, {
    status: resp.status,
    headers: {
      "content-type": resp.headers.get("content-type") || "text/plain"
    }
  });
}
