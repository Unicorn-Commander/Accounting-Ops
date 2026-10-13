// Server-side + shared types/fetcher for the Form 1120 tax package.
//
// The cpa-export service exposes:
//   GET /exports/1120/{year}?workspace_id=&entity_id=
// which returns a JSON package documented in
//   accounting-ops-cpa-export/cpa_export/reports/form_1120.py
//
// workspace_id + entity_id are query params (matching the convention of every
// other /exports + /reports endpoint). The authorize_export dependency reads
// them from the URL and authorizes against the bearer-token JWT.
//
// The app API calls cpa-export through its assertion-aware Python client. The
// web server calls that API with the user's bearer so the canonical app subject
// and workspace membership are used before the sidecar request.

import { auth } from "@/lib/auth";

const INTERNAL_BASE =
  process.env.INTERNAL_API_BASE_URL ??
  process.env.NEXT_PUBLIC_API_BASE_URL ??
  "http://app:8000";

// ---------------------------------------------------------------------------
// Types — mirror the shape documented in form_1120.py
// ---------------------------------------------------------------------------

export type Form1120Line = {
  line: string;
  label: string;
  amount_cents: number;
  accounts: string[];
};

export type Form1120LineDetail = {
  category: string;
  amount_cents: number;
  entry_count: number;
};

export type Form1120Package = {
  disclaimer: string;
  workspace_id: string;
  entity_id: string;
  tax_year: number;
  generated_at: string;
  income: {
    lines: Form1120Line[];
    total_income_cents: number;
  };
  deductions: {
    lines: Form1120Line[];
    total_deductions_cents: number;
  };
  taxable_income_before_nol_cents: number;
  schedule_l: {
    assets: Form1120Line[];
    total_assets_cents: number;
    liabilities_equity: Form1120Line[];
    total_liabilities_equity_cents: number;
    note?: string;
  };
  line_detail: Record<string, Form1120LineDetail[]>;
};

// ---------------------------------------------------------------------------
// CPA-blessing categories (from tax_mapping_1120.py)
// These warrant a yellow info badge in the UI.
// ---------------------------------------------------------------------------

export const CPA_REVIEW_CATEGORIES: Record<string, string> = {
  "Banking · Interest":
    "§163(j) interest limitation may apply. Banking interest can be income (line 5) or " +
    "deductible interest expense (line 18) depending on sign. CPA must verify routing and " +
    "confirm business interest is not subject to the §163(j) cap.",
  "SaaS · AI":
    "AI API and model costs may qualify as COGS if directly tied to a revenue-generating " +
    "product rather than internal overhead. CPA should determine the correct split between " +
    "Other deductions (line 26) and Cost of goods sold (Form 1125-A).",
  "Inter-entity transfer":
    "Inter-entity transfers may be management fees, intercompany loan repayments, or " +
    "consolidated-return eliminations — each with different tax treatment. CPA must classify " +
    "each transaction individually before filing. Line 26 is a placeholder only.",
  "Contractor · 1099-NEC":
    "If any 1099-NEC payments are to an officer of the corporation, they should be reported " +
    "on Form 1125-E (Compensation of Officers) rather than Other deductions. CPA must verify " +
    "payee status before finalizing."
};

// ---------------------------------------------------------------------------
// Server-side fetcher — calls the authenticated app API, which then uses the
// shared assertion-aware cpa-export client.
// ---------------------------------------------------------------------------

export function buildTax1120ApiUrl(
  workspaceId: string,
  entityId: string,
  year: number,
): string {
  const workspacePath = encodeURIComponent(workspaceId);
  if (entityId === "all" || entityId === "consolidated") {
    return `${INTERNAL_BASE}/api/workspaces/${workspacePath}/consolidated/tax/${year}/form-1120`;
  }
  return `${INTERNAL_BASE}/api/workspaces/${workspacePath}/entities/${encodeURIComponent(entityId)}/tax/${year}/form-1120`;
}

export function taxPackageFailureHint(fetchError: string): string {
  const normalized = fetchError.toLowerCase();
  if (
    normalized.includes("cpa-export 403") ||
    normalized.includes("http 403") ||
    normalized.includes("no active membership") ||
    normalized.includes("denied access")
  ) {
    return "This is an identity/membership mismatch between the app and cpa-export, not a missing journal-entry problem.";
  }
  if (normalized.includes("identity assertion unavailable")) {
    return "The app could not create the cpa-export identity assertion. Contact an operator; the request was stopped before data was fetched.";
  }
  return "Check that the cpa-export service is available and try again.";
}

export async function fetch1120Package(
  workspaceId: string,
  entityId: string,
  year: number
): Promise<Form1120Package> {
  const session = await auth();
  if (!session?.accessToken) {
    throw new Error("unauthenticated");
  }

  const url = buildTax1120ApiUrl(workspaceId, entityId, year);

  const res = await fetch(url, {
    headers: {
      Authorization: `Bearer ${session.accessToken}`,
      Accept: "application/json"
    },
    cache: "no-store"
  });

  if (!res.ok) {
    const text = await res.text().catch(() => "");
    let detail = text;
    try {
      const body = JSON.parse(text) as { detail?: string; error?: string };
      detail = body.detail ?? body.error ?? text;
    } catch {
      // Preserve a non-JSON upstream response verbatim below.
    }
    throw new Error(`cpa-export ${res.status}: ${detail}`);
  }

  return (await res.json()) as Form1120Package;
}

// ---------------------------------------------------------------------------
// Helper: verify workspace membership before proxying (mirrors proxy logic)
// ---------------------------------------------------------------------------

export async function verifyWorkspaceAccess(
  workspaceId: string,
  accessToken: string
): Promise<boolean> {
  try {
    const res = await fetch(
      `${INTERNAL_BASE}/api/workspaces/${encodeURIComponent(workspaceId)}`,
      {
        headers: {
          Authorization: `Bearer ${accessToken}`,
          Accept: "application/json"
        },
        cache: "no-store"
      }
    );
    return res.ok;
  } catch {
    return false;
  }
}
