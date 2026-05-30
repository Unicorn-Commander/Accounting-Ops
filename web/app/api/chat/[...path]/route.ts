// Authenticated proxy from the browser → FastAPI chat endpoints.
//
// Browser → /api/chat/{...path}
//        → http://app:8000/api/<rewritten path>
//
// The browser never speaks directly to the docker-internal FastAPI service
// and the backend needs the user's Keycloak access token (so the MCP
// tools the chat dispatches see the SAME caller as a regular API request).
// We forward Authorization: Bearer <session.accessToken> and stream the
// response body straight through — critical for the SSE message endpoint.
//
// URL convention here is a thin shim over the backend:
//   /api/chat/personas
//     → /api/chat/personas
//   /api/chat/workspaces/{wid}/sessions
//     → /api/workspaces/{wid}/chat/sessions
//   /api/chat/workspaces/{wid}/sessions/{sid}
//     → /api/workspaces/{wid}/chat/sessions/{sid}
//   /api/chat/workspaces/{wid}/sessions/{sid}/messages
//     → /api/workspaces/{wid}/chat/sessions/{sid}/messages   (SSE)
//   /api/chat/workspaces/{wid}/sessions/{sid}/cancel
//   /api/chat/workspaces/{wid}/sessions/{sid}/save-as-memo
//
// Browser-facing path is grouped under /api/chat/* so a single allow-listed
// proxy handles every chat HTTP call.

import { auth } from "@/lib/auth";
import { type NextRequest, NextResponse } from "next/server";

const INTERNAL_API_BASE =
  process.env.INTERNAL_API_BASE_URL ??
  process.env.NEXT_PUBLIC_API_BASE_URL ??
  "http://app:8000";

// Headers we copy 1:1 from upstream → client. SSE needs content-type:
// text/event-stream and cache-control to keep flushing happening.
const PASSTHROUGH_HEADERS = new Set([
  "content-type",
  "cache-control",
  "x-accel-buffering",
  "connection",
]);

function rewritePath(segments: string[]): string | null {
  if (segments.length === 0) return null;

  // /api/chat/personas
  if (segments.length === 1 && segments[0] === "personas") {
    return `/api/chat/personas`;
  }

  // /api/chat/workspaces/{wid}/sessions...
  if (segments[0] !== "workspaces" || segments.length < 3) return null;
  if (segments[2] !== "sessions") return null;

  const wid = segments[1];
  // UUID sanity check
  if (!/^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i.test(wid)) {
    return null;
  }

  const tail = segments.slice(3).join("/");
  const base = `/api/workspaces/${wid}/chat/sessions`;

  if (tail === "") return base;
  // Anything under sessions/{sid}/...
  const allowedTails = new Set(["messages", "cancel", "save-as-memo"]);
  const parts = tail.split("/");
  if (parts.length === 1) {
    // /sessions/{sid}
    return `${base}/${parts[0]}`;
  }
  if (parts.length === 2 && allowedTails.has(parts[1])) {
    return `${base}/${parts[0]}/${parts[1]}`;
  }
  return null;
}

async function proxy(
  req: NextRequest,
  segments: string[],
  method: "GET" | "POST"
): Promise<NextResponse | Response> {
  const session = await auth();
  if (!session?.user || !session.accessToken) {
    return NextResponse.json({ error: "unauthenticated" }, { status: 401 });
  }

  const backendPath = rewritePath(segments);
  if (!backendPath) {
    return NextResponse.json({ error: "not_found" }, { status: 404 });
  }

  const search = req.nextUrl.searchParams.toString();
  const url = `${INTERNAL_API_BASE}${backendPath}${search ? "?" + search : ""}`;

  // Body passthrough — only for POST.
  let body: string | undefined;
  if (method === "POST") {
    body = await req.text();
  }

  let upstream: Response;
  try {
    upstream = await fetch(url, {
      method,
      headers: {
        Authorization: `Bearer ${session.accessToken}`,
        // Always send + accept JSON for non-stream calls.  The SSE endpoint
        // ignores Accept and responds with text/event-stream regardless.
        "Content-Type": "application/json",
        Accept: "application/json, text/event-stream",
      },
      body,
      cache: "no-store",
      // Disable Next.js fetch caching + let the body stream through.
      // @ts-ignore (Next.js extension)
      duplex: "half",
    });
  } catch (err) {
    console.error("[chat proxy] upstream fetch failed:", err);
    return NextResponse.json({ error: "upstream_unreachable" }, { status: 502 });
  }

  // Build a response that streams the upstream body untouched.
  const headers = new Headers();
  upstream.headers.forEach((value, key) => {
    if (PASSTHROUGH_HEADERS.has(key.toLowerCase())) {
      headers.set(key, value);
    }
  });

  // For SSE we set sensible defaults if upstream forgot them.
  const ct = upstream.headers.get("content-type") ?? "";
  if (ct.startsWith("text/event-stream")) {
    headers.set("Cache-Control", "no-cache, no-transform");
    headers.set("X-Accel-Buffering", "no");
    headers.set("Connection", "keep-alive");
  }

  return new Response(upstream.body, { status: upstream.status, headers });
}

export async function GET(
  req: NextRequest,
  { params }: { params: Promise<{ path: string[] }> }
) {
  const { path } = await params;
  return proxy(req, path, "GET");
}

export async function POST(
  req: NextRequest,
  { params }: { params: Promise<{ path: string[] }> }
) {
  const { path } = await params;
  return proxy(req, path, "POST");
}
