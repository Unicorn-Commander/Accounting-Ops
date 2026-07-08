// Server-side fetcher + types for the tax-status dashboard endpoint.
//
// Fetches GET /api/workspaces/{wid}/tax-status directly from the FastAPI
// backend using the signed-in user's Keycloak access token. Used in the
// server component /taxes/page.tsx.

import { auth } from "@/lib/auth";

const INTERNAL_BASE =
  process.env.INTERNAL_API_BASE_URL ??
  process.env.NEXT_PUBLIC_API_BASE_URL ??
  "http://app:8000";

// ---------------------------------------------------------------------------
// Types
// ---------------------------------------------------------------------------

export type FilingStatus =
  | "overdue"
  | "draft"
  | "cpa_review"
  | "filed"
  | "no_draft";

export type BooksStatus = "complete" | "in_progress" | "no_data";

export type TaxReviewQueueSummary = {
  pending: number;
  reviewed: number;
  resolved: number;
};

export type LastSpecialistRun = {
  specialist_id: string;
  started_at: string;
  status: string;
};

export type TaxYearStatus = {
  tax_year: number;
  is_short_year: boolean;
  short_year_start: string | null;
  books_status: BooksStatus;
  pending_approvals: number;
  filing_status: FilingStatus;
  federal_filing_deadline: string;
  state_filing_deadline: string;
  last_specialist_run: LastSpecialistRun | null;
  tax_review_queue_summary: TaxReviewQueueSummary;
};

export type EntityTaxStatus = {
  entity_id: string;
  entity_slug: string;
  display_name: string;
  entity_type: string;
  jurisdictions: string[];
  incorporated_at: string | null;
  years: TaxYearStatus[];
};

export type TaxStatusResponse = {
  as_of: string;
  entities: EntityTaxStatus[];
};

// ---------------------------------------------------------------------------
// Fetcher
// ---------------------------------------------------------------------------

export async function fetchTaxStatus(
  workspaceId: string,
  opts: { asOf?: string } = {}
): Promise<TaxStatusResponse | null> {
  const session = await auth();
  if (!session?.accessToken) return null;

  const params = new URLSearchParams();
  if (opts.asOf) params.set("as_of", opts.asOf);

  try {
    const res = await fetch(
      `${INTERNAL_BASE}/api/workspaces/${workspaceId}/tax-status${params.toString() ? `?${params}` : ""}`,
      {
        headers: {
          Authorization: `Bearer ${session.accessToken}`,
          Accept: "application/json"
        },
        cache: "no-store"
      }
    );
    if (!res.ok) return null;
    return (await res.json()) as TaxStatusResponse;
  } catch {
    return null;
  }
}

// ---------------------------------------------------------------------------
// Derived helpers (used by the dashboard component)
// ---------------------------------------------------------------------------

/** Count entities with at least one overdue year. */
export function countOverdueEntities(entities: EntityTaxStatus[]): number {
  return entities.filter((e) =>
    e.years.some((y) => y.filing_status === "overdue")
  ).length;
}

/** Count total entity-years in a given filing_status. */
export function countByFilingStatus(
  entities: EntityTaxStatus[],
  status: FilingStatus
): number {
  return entities.reduce(
    (acc, e) => acc + e.years.filter((y) => y.filing_status === status).length,
    0
  );
}

/** Return all overdue entity-year pairs for the banner. */
export type OverdueItem = {
  entity: EntityTaxStatus;
  year: TaxYearStatus;
};

export function getOverdueItems(entities: EntityTaxStatus[]): OverdueItem[] {
  const items: OverdueItem[] = [];
  for (const entity of entities) {
    for (const year of entity.years) {
      if (year.filing_status === "overdue") {
        items.push({ entity, year });
      }
    }
  }
  // Sort: most recent year first
  return items.sort((a, b) => b.year.tax_year - a.year.tax_year);
}
