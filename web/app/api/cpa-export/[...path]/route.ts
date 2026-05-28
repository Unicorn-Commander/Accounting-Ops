// Authenticated proxy to the cpa-export service (docker-internal: http://cpa-export:8093).
//
// Browser → /api/cpa-export/{...path}?workspace_id=...&period=...&entity_id=...
//        → cpa-export:8093/{...path}?<same>
//
// Why proxy (not direct):
//  - The browser must not reach the docker-internal cpa-export service directly.
//  - We forward the USER's own Keycloak access token; cpa-export validates it +
//    the user's workspace membership (finding H1). We also verify the user's
//    workspace access here against the main API first (defense in depth). The
//    old shared CPA_EXPORT_SERVICE_TOKEN is no longer used — a single broad
//    token can't express per-user, per-workspace authorization.

import { auth } from "@/lib/auth";
import { type NextRequest, NextResponse } from "next/server";

const INTERNAL_API_BASE =
  process.env.INTERNAL_API_BASE_URL ??
  process.env.NEXT_PUBLIC_API_BASE_URL ??
  "http://app:8000";

const CPA_EXPORT_INTERNAL_URL =
  process.env.CPA_EXPORT_INTERNAL_URL ?? "http://cpa-export:8093";

// Allow-list of backend path segments. Anything outside this never gets proxied.
// Fixed paths are matched exactly. Dynamic 1120 paths are matched by regex below.
const ALLOWED_PATHS = new Set<string>([
  "exports/csv",
  "exports/iif",
  "exports/qbo",
  "reports/pl.pdf",
  "reports/balance-sheet.pdf",
  "reports/cash-flow.pdf",
  "healthz"
]);

// Dynamic path pattern: exports/1120/{workspace_id}/{entity_id}/{year}
// workspace_id and entity_id are UUIDs or "all"/"consolidated"; year is 4-digit.
const DYNAMIC_1120_RX =
  /^exports\/1120\/[0-9a-f-]{8,}\/[0-9a-f-]{8,}\/\d{4}$|^exports\/1120\/[^/]+\/[^/]+\/\d{4}$/;

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
  const isAllowed = ALLOWED_PATHS.has(joined) || DYNAMIC_1120_RX.test(joined);
  if (!isAllowed) {
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
      // Forward the USER's Keycloak token — cpa-export (H1) authorizes the real
      // user against workspace membership, not a shared privileged token.
      headers: { Authorization: `Bearer ${session.accessToken}` },
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
