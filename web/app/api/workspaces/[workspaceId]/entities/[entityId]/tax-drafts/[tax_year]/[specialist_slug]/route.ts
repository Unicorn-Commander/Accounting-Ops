// Pass-through proxy: GET specialist draft PDF or Markdown from the FastAPI backend.
//
// Browser → /api/workspaces/{wid}/entities/{eid}/tax-drafts/{year}/{slug}.pdf
//         → app:8000/api/workspaces/{wid}/entities/{eid}/tax-drafts/{year}/{slug}.pdf
//
// Browser → /api/workspaces/{wid}/entities/{eid}/tax-drafts/{year}/{slug}.md
//         → app:8000/api/workspaces/{wid}/entities/{eid}/tax-drafts/{year}/{slug}.md
//
// The proxy is extension-agnostic: whatever suffix the client requests
// (e.g. ".pdf", ".md") is forwarded verbatim to the backend.
//
// Query params (e.g. ?disposition=inline) are forwarded to the backend.
//
// Preserves Content-Disposition, Content-Type, and X-Draft-Notice headers.

import { auth } from "@/lib/auth";
import { type NextRequest, NextResponse } from "next/server";

const INTERNAL_BASE =
  process.env.INTERNAL_API_BASE_URL ??
  process.env.NEXT_PUBLIC_API_BASE_URL ??
  "http://app:8000";

const PASSTHROUGH_HEADERS = new Set([
  "content-type",
  "content-disposition",
  "content-length",
  "etag",
  "last-modified",
  "x-draft-notice",
  "cache-control",
]);

export async function GET(
  req: NextRequest,
  ctx: {
    params: Promise<{
      workspaceId: string;
      entityId: string;
      tax_year: string;
      specialist_slug: string;
    }>;
  }
) {
  const session = await auth();
  if (!session?.accessToken) {
    return NextResponse.json({ error: "Unauthorized" }, { status: 401 });
  }

  const {
    workspaceId,
    entityId,
    tax_year,
    specialist_slug: slugWithExt,
  } = await ctx.params;

  // The folder is [specialist_slug]; the URL includes the file extension
  // (e.g. "form-1120.pdf" or "form-1120.md") captured in the segment value.
  // We forward the full value verbatim to the backend so it reaches the correct
  // .pdf or .md route.
  //
  // Also forward query params (e.g. ?disposition=inline).
  const qs = req.nextUrl.searchParams.toString();
  const upstream =
    `${INTERNAL_BASE}/api/workspaces/${workspaceId}/entities/${entityId}` +
    `/tax-drafts/${tax_year}/${slugWithExt}` +
    (qs ? `?${qs}` : "");

  let resp: Response;
  try {
    resp = await fetch(upstream, {
      headers: { Authorization: `Bearer ${session.accessToken}` },
      cache: "no-store",
    });
  } catch (err) {
    console.error("[specialist-draft proxy] upstream fetch failed:", err);
    return NextResponse.json({ error: "upstream_unreachable" }, { status: 502 });
  }

  if (!resp.ok) {
    const text = await resp.text().catch(() => "");
    // Try to parse as JSON for a nicer error body.
    let body = text || `upstream ${resp.status}`;
    try {
      const parsed = JSON.parse(text);
      body = parsed.detail ?? body;
    } catch {
      // not JSON — use raw text
    }
    return new NextResponse(body, {
      status: resp.status,
      headers: { "content-type": "text/plain" },
    });
  }

  const headers = new Headers();
  resp.headers.forEach((value, key) => {
    if (PASSTHROUGH_HEADERS.has(key.toLowerCase())) {
      headers.set(key, value);
    }
  });

  return new NextResponse(resp.body, { status: 200, headers });
}
