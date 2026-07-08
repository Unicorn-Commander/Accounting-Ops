// Generic authenticated proxy to the FastAPI backend. Any request to
// /api/proxy/<...> is forwarded to ${INTERNAL_BASE}/<...> with the caller's
// uchub access token attached as a Bearer. Used by client-side components that
// need to call the backend without us writing a route per endpoint.
//
// The proxy ONLY forwards within /api/* on the backend (path prefix check)
// to avoid being abused as an open proxy to internal services.

import { NextRequest, NextResponse } from "next/server";
import { auth } from "@/lib/auth";

const INTERNAL_BASE =
  process.env.INTERNAL_API_BASE_URL ??
  process.env.NEXT_PUBLIC_API_BASE_URL ??
  "http://app:8000";

async function handle(
  req: NextRequest,
  ctx: { params: Promise<{ path: string[] }> }
) {
  const session = await auth();
  if (!session?.accessToken) {
    return NextResponse.json({ error: "Unauthorized" }, { status: 401 });
  }
  const { path } = await ctx.params;
  // path[] is the segments after /api/proxy/. We expect the caller to include
  // the leading "api" so a call like /api/proxy/api/workspaces/... maps to
  // /api/workspaces/... on the backend.
  const joined = "/" + path.map(encodeURIComponent).join("/");
  if (!joined.startsWith("/api/")) {
    return NextResponse.json(
      { error: "Proxy only forwards under /api/" },
      { status: 400 }
    );
  }
  const search = req.nextUrl.search || "";
  const url = `${INTERNAL_BASE}${joined}${search}`;

  const init: RequestInit = {
    method: req.method,
    headers: {
      Authorization: `Bearer ${session.accessToken}`,
      "Content-Type": req.headers.get("content-type") || "application/json",
      Accept: "application/json",
    },
    cache: "no-store",
  };
  if (req.method !== "GET" && req.method !== "HEAD") {
    init.body = await req.text();
  }

  const res = await fetch(url, init);
  const body = await res.text();
  return new NextResponse(body, {
    status: res.status,
    headers: {
      "Content-Type": res.headers.get("content-type") || "application/json",
    },
  });
}

export async function GET(
  req: NextRequest,
  ctx: { params: Promise<{ path: string[] }> }
) {
  return handle(req, ctx);
}
export async function POST(
  req: NextRequest,
  ctx: { params: Promise<{ path: string[] }> }
) {
  return handle(req, ctx);
}
export async function PUT(
  req: NextRequest,
  ctx: { params: Promise<{ path: string[] }> }
) {
  return handle(req, ctx);
}
export async function PATCH(
  req: NextRequest,
  ctx: { params: Promise<{ path: string[] }> }
) {
  return handle(req, ctx);
}
export async function DELETE(
  req: NextRequest,
  ctx: { params: Promise<{ path: string[] }> }
) {
  return handle(req, ctx);
}
