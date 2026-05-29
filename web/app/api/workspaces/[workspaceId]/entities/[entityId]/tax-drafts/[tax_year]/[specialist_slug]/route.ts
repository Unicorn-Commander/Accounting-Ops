// Pass-through proxy: GET specialist draft PDF from the FastAPI backend.
//
// Browser → /api/workspaces/{wid}/entities/{eid}/tax-drafts/{year}/{slug}.pdf
//         → app:8000/api/workspaces/{wid}/entities/{eid}/tax-drafts/{year}/{slug}.pdf
//
// Preserves Content-Disposition and Content-Type so the browser triggers a
// native file download rather than trying to display JSON.

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
]);

export async function GET(
  _req: NextRequest,
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
    specialist_slug: slugPdf,
  } = await ctx.params;

  // The folder was originally [specialist_slug].pdf but Next.js can't handle
  // "." in a segment name. Folder is [specialist_slug]; the URL still includes
  // the literal ".pdf" suffix in the captured value (e.g. "form-1120.pdf"),
  // which we forward to the backend route which expects {slug}.pdf in its path.
  const upstream = `${INTERNAL_BASE}/api/workspaces/${workspaceId}/entities/${entityId}/tax-drafts/${tax_year}/${slugPdf}`;

  let resp: Response;
  try {
    resp = await fetch(upstream, {
      headers: { Authorization: `Bearer ${session.accessToken}` },
      cache: "no-store",
    });
  } catch (err) {
    console.error("[specialist-pdf proxy] upstream fetch failed:", err);
    return NextResponse.json({ error: "upstream_unreachable" }, { status: 502 });
  }

  if (!resp.ok) {
    const text = await resp.text().catch(() => "");
    return new NextResponse(text || `upstream ${resp.status}`, {
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
