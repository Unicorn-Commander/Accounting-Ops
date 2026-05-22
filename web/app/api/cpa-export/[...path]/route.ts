// Authenticated proxy to the cpa-export service (docker-internal: http://cpa-export:8093).
//
// Browser → /api/cpa-export/{...path}?workspace_id=...&period=...&entity_id=...
//        → cpa-export:8093/{...path}?<same>
//
// Why proxy (not direct):
//  - CPA_EXPORT_SERVICE_TOKEN is a privileged service token with broad access.
//    It must never reach the browser.
//  - We verify the calling user's session + the user's workspace access before
//    forwarding (defense in depth).

import { auth } from "@/lib/auth";
import { type NextRequest, NextResponse } from "next/server";

const INTERNAL_API_BASE =
  process.env.INTERNAL_API_BASE_URL ??
  process.env.NEXT_PUBLIC_API_BASE_URL ??
  "http://app:8000";

const CPA_EXPORT_INTERNAL_URL =
  process.env.CPA_EXPORT_INTERNAL_URL ?? "http://cpa-export:8093";

const CPA_EXPORT_SERVICE_TOKEN = process.env.CPA_EXPORT_SERVICE_TOKEN ?? "";

// Allow-list of backend path segments. Anything outside this never gets proxied.
const ALLOWED_PATHS = new Set<string>([
  "exports/csv",
  "exports/iif",
  "exports/qbo",
  "reports/pl.pdf",
  "reports/balance-sheet.pdf",
  "reports/cash-flow.pdf",
  "healthz"
]);

const PASSTHROUGH_HEADERS = new Set([
  "content-type",
  "content-disposition",
  "content-length",
  "etag",
  "last-modified"
]);

export async function GET(
  req: NextRequest,
  { params }: { params: Promise<{ path: string[] }> }
) {
  const session = await auth();
  if (!session?.user || !session.accessToken) {
    return NextResponse.json({ error: "unauthenticated" }, { status: 401 });
  }

  const { path } = await params;
  const joined = path.join("/");
  if (!ALLOWED_PATHS.has(joined)) {
    return NextResponse.json({ error: "not_found" }, { status: 404 });
  }

  // healthz needs no workspace; everything else does.
  if (joined !== "healthz") {
    const workspaceId = req.nextUrl.searchParams.get("workspace_id");
    if (!workspaceId) {
      return NextResponse.json(
        { error: "workspace_id required" },
        { status: 400 }
      );
    }

    // Verify the calling user actually has access to this workspace
    // by hitting the main API as the user (their Keycloak access token).
    try {
      const wsCheck = await fetch(
        `${INTERNAL_API_BASE}/api/workspaces/${encodeURIComponent(workspaceId)}`,
        {
          headers: {
            Authorization: `Bearer ${session.accessToken}`,
            Accept: "application/json"
          },
          cache: "no-store"
        }
      );
      if (!wsCheck.ok) {
        return NextResponse.json(
          { error: "forbidden" },
          { status: wsCheck.status === 404 ? 404 : 403 }
        );
      }
    } catch (err) {
      console.error("[cpa-export proxy] workspace check failed:", err);
      return NextResponse.json({ error: "upstream_unreachable" }, { status: 502 });
    }
  }

  const search = req.nextUrl.searchParams.toString();
  const targetUrl = `${CPA_EXPORT_INTERNAL_URL}/${joined}${search ? "?" + search : ""}`;

  let upstream: Response;
  try {
    upstream = await fetch(targetUrl, {
      headers: { Authorization: `Bearer ${CPA_EXPORT_SERVICE_TOKEN}` },
      cache: "no-store"
    });
  } catch (err) {
    console.error("[cpa-export proxy] upstream fetch failed:", err);
    return NextResponse.json({ error: "upstream_unreachable" }, { status: 502 });
  }

  if (!upstream.ok) {
    const text = await upstream.text().catch(() => "");
    return new NextResponse(text || `upstream ${upstream.status}`, {
      status: upstream.status,
      headers: { "content-type": "text/plain" }
    });
  }

  const headers = new Headers();
  upstream.headers.forEach((value, key) => {
    if (PASSTHROUGH_HEADERS.has(key.toLowerCase())) {
      headers.set(key, value);
    }
  });

  // Stream upstream body straight through — don't buffer.
  return new NextResponse(upstream.body, { status: 200, headers });
}
