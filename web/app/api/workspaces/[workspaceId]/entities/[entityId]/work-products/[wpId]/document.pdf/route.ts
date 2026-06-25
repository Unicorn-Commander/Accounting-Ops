/**
 * Web proxy: W-4P (and any work-product) filled-PDF re-render.
 *
 * Browser → /api/workspaces/{wid}/entities/{eid}/work-products/{id}/document.pdf
 *        → app:8000/api/workspaces/{wid}/entities/{eid}/work-products/{id}/document.pdf
 *
 * The backend re-renders the work product to application/pdf ON DEMAND from the
 * row's stored inputs (watermark by status) and streams it back. We must stream
 * the body straight through and NOT buffer it as text — the generic
 * /api/proxy route does `await res.text()`, which corrupts binary PDF bytes, so
 * work-product PDFs get their own binary-safe route (same shape as irs-forms).
 *
 * The folder is named "document.pdf" so the browser-facing URL ends in a literal
 * ".pdf" the browser recognizes; the upstream sets the real content-type.
 *
 * Query params forwarded:
 *   disposition  inline | attachment  (default inline)
 */

import { type NextRequest, NextResponse } from "next/server";
import { auth } from "@/lib/auth";

const INTERNAL_BASE =
  process.env.INTERNAL_API_BASE_URL ??
  process.env.NEXT_PUBLIC_API_BASE_URL ??
  "http://app:8000";

const PASSTHROUGH_HEADERS = new Set([
  "content-type",
  "content-disposition",
  "content-length",
  "x-draft-notice",
  "cache-control",
]);

export async function GET(
  req: NextRequest,
  ctx: {
    params: Promise<{
      workspaceId: string;
      entityId: string;
      wpId: string;
    }>;
  }
): Promise<NextResponse | Response> {
  const session = await auth();
  if (!session?.user || !session.accessToken) {
    return NextResponse.json({ error: "unauthenticated" }, { status: 401 });
  }

  const { workspaceId, entityId, wpId } = await ctx.params;

  const backendPath =
    `/api/workspaces/${encodeURIComponent(workspaceId)}` +
    `/entities/${encodeURIComponent(entityId)}` +
    `/work-products/${encodeURIComponent(wpId)}/document.pdf`;

  // Forward the disposition param (inline for the iframe preview; attachment
  // when the caller wants a download). Default inline, matching the backend.
  const qs = new URLSearchParams();
  const disp = req.nextUrl.searchParams.get("disposition");
  qs.set("disposition", disp === "attachment" ? "attachment" : "inline");

  const targetUrl = `${INTERNAL_BASE}${backendPath}?${qs.toString()}`;

  let upstream: Response;
  try {
    upstream = await fetch(targetUrl, {
      headers: { Authorization: `Bearer ${session.accessToken}` },
      cache: "no-store",
    });
  } catch (err) {
    console.error("[work-products pdf proxy] upstream fetch failed:", err);
    return NextResponse.json({ error: "upstream_unreachable" }, { status: 502 });
  }

  if (!upstream.ok) {
    const text = await upstream.text().catch(() => "");
    let detail = text;
    try {
      const parsed = JSON.parse(text);
      detail = parsed.detail ?? text;
    } catch {
      // not JSON
    }
    return NextResponse.json(
      { error: detail || `upstream ${upstream.status}` },
      { status: upstream.status }
    );
  }

  // Stream the binary body straight through.
  const headers = new Headers();
  upstream.headers.forEach((value, key) => {
    if (PASSTHROUGH_HEADERS.has(key.toLowerCase())) {
      headers.set(key, value);
    }
  });

  return new NextResponse(upstream.body, {
    status: 200,
    headers,
  });
}
