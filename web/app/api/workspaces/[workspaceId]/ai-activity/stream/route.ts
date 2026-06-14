// Proxy: GET /api/workspaces/{workspaceId}/ai-activity/stream
// Streams the FastAPI agent-activity SSE through to the browser, attaching the
// user's Keycloak bearer server-side (a browser fetch/EventSource can't set it).
// Mirrors the chat SSE proxy (web/app/api/chat/[...path]/route.ts).

import { NextRequest, NextResponse } from "next/server";
import { auth } from "@/lib/auth";

export const dynamic = "force-dynamic";

const INTERNAL_BASE =
  process.env.INTERNAL_API_BASE_URL ??
  process.env.NEXT_PUBLIC_API_BASE_URL ??
  "http://app:8000";

// Only forward SSE-relevant headers upstream → client.
const PASSTHROUGH_HEADERS = new Set([
  "content-type",
  "cache-control",
  "x-accel-buffering",
  "connection"
]);

export async function GET(
  req: NextRequest,
  ctx: { params: Promise<{ workspaceId: string }> }
) {
  const session = await auth();
  if (!session?.accessToken) {
    return NextResponse.json({ error: "Unauthorized" }, { status: 401 });
  }
  const { workspaceId } = await ctx.params;
  const qs = req.nextUrl.searchParams.toString();

  let upstream: Response;
  try {
    upstream = await fetch(
      `${INTERNAL_BASE}/api/workspaces/${workspaceId}/ai-activity/stream${
        qs ? "?" + qs : ""
      }`,
      {
        headers: {
          Authorization: `Bearer ${session.accessToken}`,
          Accept: "text/event-stream"
        },
        cache: "no-store",
        // Abort the upstream SSE when the browser disconnects, so the FastAPI
        // generator cancels cleanly instead of leaking a connection.
        signal: req.signal
      }
    );
  } catch {
    return NextResponse.json({ error: "upstream_unreachable" }, { status: 502 });
  }

  if (!upstream.ok || !upstream.body) {
    return NextResponse.json(
      { error: "upstream_error" },
      { status: upstream.status || 502 }
    );
  }

  const headers = new Headers();
  upstream.headers.forEach((value, key) => {
    if (PASSTHROUGH_HEADERS.has(key.toLowerCase())) headers.set(key, value);
  });
  // Ensure SSE-correct headers even if upstream omitted any.
  headers.set("Content-Type", "text/event-stream");
  headers.set("Cache-Control", "no-cache, no-transform");
  headers.set("X-Accel-Buffering", "no");
  headers.set("Connection", "keep-alive");

  return new Response(upstream.body, { status: 200, headers });
}
