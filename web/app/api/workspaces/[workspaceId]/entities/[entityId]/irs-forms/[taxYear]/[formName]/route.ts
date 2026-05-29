/**
 * Web proxy: IRS form pre-fill PDFs.
 *
 * Browser → /api/workspaces/{wid}/entities/{eid}/irs-forms/{year}/{name}.pdf
 *        → app:8000/api/workspaces/{wid}/entities/{eid}/irs-forms/{year}/{name}.pdf
 *
 * The backend endpoint returns application/pdf (single form) or
 * application/zip (1120-package). We stream the response body straight through
 * to the browser without buffering — safe because these are < 5 MB files.
 *
 * Supported form names (before .pdf suffix):
 *   1120              → Form 1120 income/deductions
 *   1120-schedule-l   → Schedule L balance sheet
 *   1125-e            → Form 1125-E officer compensation
 *   1120-package      → zip of all three
 *
 * The route file is named [formName].pdf so that the Next.js App Router exposes
 * the URL with a literal ".pdf" extension that the browser recognizes as a
 * download. The actual content-type is set by the upstream response.
 *
 * Query params forwarded as-is to the backend:
 *   include_queue_overrides  (bool, default true)
 */

import { type NextRequest, NextResponse } from "next/server";
import { auth } from "@/lib/auth";

const INTERNAL_BASE =
  process.env.INTERNAL_API_BASE_URL ??
  process.env.NEXT_PUBLIC_API_BASE_URL ??
  "http://app:8000";

const SUPPORTED_FORMS = new Set([
  "1120",
  "1120-schedule-l",
  "1125-e",
  "1120-package",
]);

const VALID_YEAR_RX = /^\d{4}$/;

const PASSTHROUGH_HEADERS = new Set([
  "content-type",
  "content-disposition",
  "content-length",
  "x-draft-notice",
  "cache-control",
]);

export async function GET(
  req: NextRequest,
  ctx: {
    params: Promise<{
      workspaceId: string;
      entityId: string;
      taxYear: string;
      formName: string;
    }>;
  }
): Promise<NextResponse | Response> {
  const session = await auth();
  if (!session?.user || !session.accessToken) {
    return NextResponse.json({ error: "unauthenticated" }, { status: 401 });
  }

  const params = await ctx.params;
  const { workspaceId, entityId, taxYear } = params;
  // The folder is named [formName] (Next.js can't handle "." in segment names);
  // the browser URL still ends in ".pdf" and that suffix is captured as part
  // of the dynamic segment value. Strip it for the form-name allowlist check.
  const formNameWithExt: string = params.formName ?? "";
  const formName = formNameWithExt.replace(/\.pdf$/i, "");

  // Validate path params
  if (!VALID_YEAR_RX.test(taxYear)) {
    return NextResponse.json(
      { error: "taxYear must be a 4-digit year" },
      { status: 400 }
    );
  }

  if (!SUPPORTED_FORMS.has(formName.toLowerCase())) {
    return NextResponse.json(
      {
        error: `Unknown form '${formName}'. Supported: ${[...SUPPORTED_FORMS].join(", ")}.`,
      },
      { status: 404 }
    );
  }

  // Build the backend URL — note the .pdf suffix in the path (required by backend)
  const backendPath =
    `/api/workspaces/${encodeURIComponent(workspaceId)}` +
    `/entities/${encodeURIComponent(entityId)}` +
    `/irs-forms/${encodeURIComponent(taxYear)}` +
    `/${encodeURIComponent(formName)}.pdf`;

  // Forward recognized query params
  const qs = new URLSearchParams();
  const iqo = req.nextUrl.searchParams.get("include_queue_overrides");
  if (iqo !== null) qs.set("include_queue_overrides", iqo);
  const disp = req.nextUrl.searchParams.get("disposition");
  if (disp !== null) qs.set("disposition", disp);

  const targetUrl = `${INTERNAL_BASE}${backendPath}${qs.size ? "?" + qs.toString() : ""}`;

  let upstream: Response;
  try {
    upstream = await fetch(targetUrl, {
      headers: {
        Authorization: `Bearer ${session.accessToken}`,
      },
      cache: "no-store",
    });
  } catch (err) {
    console.error("[irs-forms proxy] upstream fetch failed:", err);
    return NextResponse.json({ error: "upstream_unreachable" }, { status: 502 });
  }

  if (!upstream.ok) {
    const text = await upstream.text().catch(() => "");
    // Try to parse as JSON (FastAPI error detail)
    let detail = text;
    try {
      const parsed = JSON.parse(text);
      detail = parsed.detail ?? text;
    } catch {
      // not JSON
    }
    return NextResponse.json(
      { error: detail || `upstream ${upstream.status}` },
      { status: upstream.status }
    );
  }

  // Stream the binary body through
  const headers = new Headers();
  upstream.headers.forEach((value, key) => {
    if (PASSTHROUGH_HEADERS.has(key.toLowerCase())) {
      headers.set(key, value);
    }
  });

  return new NextResponse(upstream.body, {
    status: 200,
    headers,
  });
}
