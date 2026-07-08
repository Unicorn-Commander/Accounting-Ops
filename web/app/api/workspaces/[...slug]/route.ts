/**
 * Catch-all proxy: forwards ANY /api/workspaces/* request to the FastAPI
 * app backend with the user's NextAuth bearer attached.
 *
 * Why this exists: the live Traefik (on commander) routes /api/* to the web
 * container by default. The app backend has its own /api/workspaces/*
 * endpoints but the browser-side fetch needs an Authorization header (from
 * the session cookie). This catch-all provides that bearer attachment for
 * any workspace path that doesn't have a more-specific proxy defined.
 *
 * Specific routes (e.g. open-items/document-received) still win over this
 * catch-all because Next.js route resolution prefers exact matches over
 * dynamic ones.
 */
import { NextRequest, NextResponse } from "next/server";
import { auth } from "@/lib/auth";

const INTERNAL_BASE =
  process.env.INTERNAL_API_BASE_URL ??
  process.env.NEXT_PUBLIC_API_BASE_URL ??
  "http://app:8000";

async function proxy(
  req: NextRequest,
  ctx: { params: Promise<{ slug: string[] }> },
) {
  const session = await auth();
  if (!session?.accessToken) {
    return NextResponse.json({ error: "Unauthorized" }, { status: 401 });
  }
  const { slug } = await ctx.params;
  const path = `/api/workspaces/${slug.map(encodeURIComponent).join("/")}`;
  const search = req.nextUrl.search ?? "";
  const url = `${INTERNAL_BASE}${path}${search}`;

  const headers: Record<string, string> = {
    Authorization: `Bearer ${session.accessToken}`,
    Accept: req.headers.get("accept") ?? "application/json",
  };
  const contentType = req.headers.get("content-type");
  if (contentType) headers["Content-Type"] = contentType;

  const body =
    req.method === "GET" || req.method === "HEAD"
      ? undefined
      : await req.arrayBuffer();

  let upstream: Response;
  try {
    upstream = await fetch(url, {
      method: req.method,
      headers,
      body,
      cache: "no-store",
    });
  } catch {
    return NextResponse.json({ error: "upstream_unreachable" }, { status: 502 });
  }

  // Stream the upstream response back unmodified (preserves content-type +
  // status). Strip hop-by-hop headers Next.js doesn't want.
  const outHeaders = new Headers();
  upstream.headers.forEach((value, key) => {
    const lk = key.toLowerCase();
    if (
      lk === "content-type" ||
      lk === "content-disposition" ||
      lk === "cache-control" ||
      lk.startsWith("x-")
    ) {
      outHeaders.set(key, value);
    }
  });

  return new NextResponse(upstream.body, {
    status: upstream.status,
    headers: outHeaders,
  });
}

export const GET = proxy;
export const POST = proxy;
export const PATCH = proxy;
export const PUT = proxy;
export const DELETE = proxy;
