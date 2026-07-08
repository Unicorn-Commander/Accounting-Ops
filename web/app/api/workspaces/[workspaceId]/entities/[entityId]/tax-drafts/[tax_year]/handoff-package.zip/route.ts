// Pass-through proxy: GET CPA Handoff Package ZIP from the FastAPI backend.
//
// Browser → /api/workspaces/{wid}/entities/{eid}/tax-drafts/{year}/handoff-package.zip
//         → app:8000/api/workspaces/{wid}/entities/{eid}/tax-drafts/{year}/handoff-package.zip
//
// Streams the binary ZIP response straight through. Preserves
// Content-Disposition so the browser saves with the expected filename.

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
    }>;
  }
) {
  const session = await auth();
  if (!session?.accessToken) {
    return NextResponse.json({ error: "Unauthorized" }, { status: 401 });
  }

  const { workspaceId, entityId, tax_year } = await ctx.params;
  const upstream = `${INTERNAL_BASE}/api/workspaces/${workspaceId}/entities/${entityId}/tax-drafts/${tax_year}/handoff-package.zip`;

  let resp: Response;
  try {
    resp = await fetch(upstream, {
      headers: { Authorization: `Bearer ${session.accessToken}` },
      cache: "no-store",
    });
  } catch (err) {
    console.error("[handoff-package proxy] upstream fetch failed:", err);
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
