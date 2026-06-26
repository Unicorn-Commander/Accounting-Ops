/**
 * Web proxy: upload a SIGNED PDF for a signature request (the Manual
 * download→sign→re-upload path).
 *
 * Browser (multipart/form-data, field `signed_pdf`)
 *   → /api/esign-upload/{wid}/{eid}/{wpId}/{srId}
 *   → app:8000/api/workspaces/{wid}/entities/{eid}/work-products/{wpId}
 *      /signature-requests/{srId}/mark-signed
 *
 * The generic /api/proxy route does `await req.text()` on the request body,
 * which corrupts the binary PDF inside the multipart payload — so signed-PDF
 * uploads get this dedicated binary-safe route that forwards the raw bytes
 * (arrayBuffer) with the original multipart Content-Type (boundary preserved).
 */

import { type NextRequest, NextResponse } from "next/server";
import { auth } from "@/lib/auth";

const INTERNAL_BASE =
  process.env.INTERNAL_API_BASE_URL ??
  process.env.NEXT_PUBLIC_API_BASE_URL ??
  "http://app:8000";

export async function POST(
  req: NextRequest,
  ctx: {
    params: Promise<{
      workspaceId: string;
      entityId: string;
      wpId: string;
      srId: string;
    }>;
  }
): Promise<NextResponse | Response> {
  const session = await auth();
  if (!session?.user || !session.accessToken) {
    return NextResponse.json({ error: "unauthenticated" }, { status: 401 });
  }

  const { workspaceId, entityId, wpId, srId } = await ctx.params;
  const backendPath =
    `/api/workspaces/${encodeURIComponent(workspaceId)}` +
    `/entities/${encodeURIComponent(entityId)}` +
    `/work-products/${encodeURIComponent(wpId)}` +
    `/signature-requests/${encodeURIComponent(srId)}/mark-signed`;

  // Read the raw body as bytes (binary-safe) and forward the multipart
  // Content-Type verbatim so the boundary stays intact.
  const body = await req.arrayBuffer();
  const headers: Record<string, string> = {
    Authorization: `Bearer ${session.accessToken}`,
  };
  const ct = req.headers.get("content-type");
  if (ct) headers["Content-Type"] = ct;

  let upstream: Response;
  try {
    upstream = await fetch(`${INTERNAL_BASE}${backendPath}`, {
      method: "POST",
      headers,
      body,
      cache: "no-store",
    });
  } catch (err) {
    console.error("[esign-upload proxy] upstream fetch failed:", err);
    return NextResponse.json({ error: "upstream_unreachable" }, { status: 502 });
  }

  const text = await upstream.text();
  return new NextResponse(text, {
    status: upstream.status,
    headers: {
      "Content-Type": upstream.headers.get("content-type") || "application/json",
    },
  });
}
