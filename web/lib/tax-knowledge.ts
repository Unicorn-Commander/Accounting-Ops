// Server-side client for the tax knowledge browser endpoints.
// Backend: app/api/tax_knowledge.py

import { auth } from "@/lib/auth";

const INTERNAL_BASE =
  process.env.INTERNAL_API_BASE_URL ??
  process.env.NEXT_PUBLIC_API_BASE_URL ??
  "http://app:8000";

export type KnowledgeFacets = {
  jurisdictions: Array<{ jurisdiction: string; doc_count: number }>;
  topics: Array<{ jurisdiction: string; topic: string; doc_count: number }>;
  tax_years: number[];
  total_documents: number;
  error?: string;
};

export type KnowledgeDocument = {
  id: string;
  jurisdiction: string;
  topic: string;
  tax_year: number | null;
  title: string;
  source_url: string;
  source_authority: string;
  version: number;
  is_current: boolean;
  ingested_at: string | null;
  content?: string;
  similarity?: number | null;
};

export type KnowledgeListResponse = {
  items: KnowledgeDocument[];
  total: number;
  search_mode: "browse" | "keyword" | "semantic" | "unavailable";
  query?: string | null;
  error?: string;
};

async function apiFetch<T>(path: string): Promise<T | null> {
  const session = await auth();
  if (!session?.accessToken) return null;
  try {
    const res = await fetch(`${INTERNAL_BASE}${path}`, {
      headers: {
        Authorization: `Bearer ${session.accessToken}`,
        Accept: "application/json"
      },
      cache: "no-store"
    });
    if (!res.ok) return null;
    return (await res.json()) as T;
  } catch {
    return null;
  }
}

export async function fetchKnowledgeFacets(): Promise<KnowledgeFacets | null> {
  return await apiFetch<KnowledgeFacets>("/api/tax-knowledge/topics");
}

export async function fetchKnowledgeDocuments(opts: {
  jurisdiction?: string;
  topic?: string;
  tax_year?: number;
  query?: string;
  limit?: number;
  offset?: number;
}): Promise<KnowledgeListResponse | null> {
  const params = new URLSearchParams();
  if (opts.jurisdiction) params.set("jurisdiction", opts.jurisdiction);
  if (opts.topic) params.set("topic", opts.topic);
  if (opts.tax_year != null) params.set("tax_year", String(opts.tax_year));
  if (opts.query?.trim()) params.set("query", opts.query.trim());
  if (opts.limit) params.set("limit", String(opts.limit));
  if (opts.offset) params.set("offset", String(opts.offset));
  const qs = params.toString();
  return await apiFetch<KnowledgeListResponse>(
    `/api/tax-knowledge/documents${qs ? `?${qs}` : ""}`
  );
}

export async function fetchKnowledgeDocument(
  id: string
): Promise<KnowledgeDocument | null> {
  return await apiFetch<KnowledgeDocument>(
    `/api/tax-knowledge/documents/${encodeURIComponent(id)}`
  );
}

// Friendly labels for jurisdictions
export const JURISDICTION_LABELS: Record<string, string> = {
  US: "United States (Federal)",
  CFR: "Code of Federal Regulations",
  AL: "Alabama",
  AK: "Alaska",
  AZ: "Arizona",
  AR: "Arkansas",
  CA: "California",
  CO: "Colorado",
  CT: "Connecticut",
  DE: "Delaware",
  FL: "Florida",
  GA: "Georgia",
  HI: "Hawaii",
  ID: "Idaho",
  IL: "Illinois",
  IN: "Indiana",
  IA: "Iowa",
  KS: "Kansas",
  KY: "Kentucky",
  LA: "Louisiana",
  ME: "Maine",
  MD: "Maryland",
  MA: "Massachusetts",
  MI: "Michigan",
  MN: "Minnesota",
  MS: "Mississippi",
  MO: "Missouri",
  MT: "Montana",
  NE: "Nebraska",
  NV: "Nevada",
  NH: "New Hampshire",
  NJ: "New Jersey",
  NM: "New Mexico",
  NY: "New York",
  NC: "North Carolina",
  ND: "North Dakota",
  OH: "Ohio",
  OK: "Oklahoma",
  OR: "Oregon",
  PA: "Pennsylvania",
  RI: "Rhode Island",
  SC: "South Carolina",
  SD: "South Dakota",
  TN: "Tennessee",
  TX: "Texas",
  UT: "Utah",
  VT: "Vermont",
  VA: "Virginia",
  WA: "Washington",
  WV: "West Virginia",
  WI: "Wisconsin",
  WY: "Wyoming",
  DC: "District of Columbia"
};

export function jurisdictionLabel(code: string): string {
  return JURISDICTION_LABELS[code] ?? code;
}

// Friendly labels for some common topics
const TOPIC_LABELS: Record<string, string> = {
  "174": "§174 R&D Capitalization",
  "163j": "§163(j) Business Interest",
  "1202": "§1202 QSBS",
  "1099_NEC": "1099-NEC",
  "1099_K": "1099-K",
  "form_1120": "Form 1120 (Corporate Return)",
  "sc1120": "SC 1120 (South Carolina)",
  "bonus_depreciation": "Bonus Depreciation",
  "tcja_obbba_tracker": "TCJA / OBBBA Tracker",
  "officer_compensation": "Officer Compensation",
  "nol": "Net Operating Losses",
  "schedule_l": "Schedule L (Balance Sheet)"
};

export function topicLabel(slug: string): string {
  return TOPIC_LABELS[slug] ?? slug.replace(/_/g, " ");
}
