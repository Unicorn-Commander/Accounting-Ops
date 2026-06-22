// Proxy: POST /api/workspaces/{workspaceId}/entities/{entityId}/documents/upload
//
// The generic /api/proxy route forces a JSON content-type and reads the body as
// text, which corrupts binary multipart uploads. This dedicated route forwards
// the multipart/form-data body through unchanged (preserving the boundary +
// raw bytes) with the caller's Keycloak bearer attached.
//
// Backend: app/api/documents.py :: upload_document

import { NextRequest, NextResponse } from "next/server";
import { auth } from "@/lib/auth";

const INTERNAL_BASE =
  process.env.INTERNAL_API_BASE_URL ??
  process.env.NEXT_PUBLIC_API_BASE_URL ??
  "http://app:8000";

// Cap mirrored from the backend default (100 MB) — reject early at the edge so a
// giant body never streams all the way to the API. Override via env to match a
// changed DOCUMENT_UPLOAD_MAX_BYTES.
const MAX_BYTES = Number(process.env.DOCUMENT_UPLOAD_MAX_BYTES ?? 100 * 1024 * 1024);

export async function POST(
  req: NextRequest,
  ctx: { params: Promise<{ workspaceId: string; entityId: string }> }
) {
  const session = await auth();
  if (!session?.accessToken) {
    return NextResponse.json({ error: "Unauthorized" }, { status: 401 });
  }
  const { workspaceId, entityId } = await ctx.params;

  const contentType = req.headers.get("content-type") || "";
  if (!contentType.toLowerCase().startsWith("multipart/form-data")) {
    return NextResponse.json(
      { detail: "Expected multipart/form-data." },
      { status: 415 }
    );
  }

  // Read the raw bytes once so we can size-check, then forward as the body.
  // (Files here are bounded by MAX_BYTES; buffering is fine for this size.)
  const buf = await req.arrayBuffer();
  if (buf.byteLength > MAX_BYTES) {
    return NextResponse.json(
      { detail: `File exceeds the ${Math.floor(MAX_BYTES / (1024 * 1024))} MB limit.` },
      { status: 413 }
    );
  }

  const url = `${INTERNAL_BASE}/api/workspaces/${workspaceId}/entities/${entityId}/documents/upload`;
  const res = await fetch(url, {
    method: "POST",
    headers: {
      Authorization: `Bearer ${session.accessToken}`,
      // Forward the original content-type INCLUDING the multipart boundary so
      // the backend parser can split the parts correctly.
      "Content-Type": contentType,
      Accept: "application/json",
    },
    body: buf,
    cache: "no-store",
  });

  const respBody = await res.text();
  return new NextResponse(respBody, {
    status: res.status,
    headers: { "Content-Type": res.headers.get("content-type") || "application/json" },
  });
}
