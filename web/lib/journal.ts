// Server-side client for journal + reports endpoints.

import { auth } from "@/lib/auth";

const INTERNAL_BASE =
  process.env.INTERNAL_API_BASE_URL ??
  process.env.NEXT_PUBLIC_API_BASE_URL ??
  "http://app:8000";

export type JournalEntry = {
  id: string;
  entity_id: string;
  entity_display_name: string | null;
  entity_slug: string | null;
  posted_at: string;
  effective_date: string;
  amount: string;
  currency_code: string;
  debit_account: string;
  credit_account: string;
  category: string | null;
  materiality_tier: "L" | "M" | "H" | null;
  source: string | null;
  source_external_id: string | null;
  description: string | null;
  payee: string | null;
  memo: string | null;
  status: "posted" | "reversed";
  identity: string | null;
  approving_human_uc_uid: string | null;
  reverses_entry_id?: string | null;
  reasoning_trace?: string | null;
  model_version?: string | null;
  prev_hash_hex: string;
  this_hash_hex: string;
};

export type JournalListResponse = {
  entries: JournalEntry[];
  total: number;
  categories: string[];
};

export type JournalFilters = {
  entityId?: string;
  startDate?: string;
  endDate?: string;
  category?: string;
  statusFilter?: string;
  search?: string;
  limit?: number;
  offset?: number;
};

export async function getJournalEntries(
  workspaceId: string,
  filters: JournalFilters = {}
): Promise<JournalListResponse | null> {
  const session = await auth();
  if (!session?.accessToken) return null;
  const params = new URLSearchParams();
  if (filters.entityId) params.set("entity_id", filters.entityId);
  if (filters.startDate) params.set("start_date", filters.startDate);
  if (filters.endDate) params.set("end_date", filters.endDate);
  if (filters.category) params.set("category", filters.category);
  if (filters.statusFilter) params.set("status_filter", filters.statusFilter);
  if (filters.search) params.set("search", filters.search);
  params.set("limit", String(filters.limit ?? 100));
  params.set("offset", String(filters.offset ?? 0));

  try {
    const res = await fetch(
      `${INTERNAL_BASE}/api/workspaces/${workspaceId}/journal-entries?${params}`,
      {
        headers: {
          Authorization: `Bearer ${session.accessToken}`,
          Accept: "application/json"
        },
        cache: "no-store"
      }
    );
    if (!res.ok) return null;
    return (await res.json()) as JournalListResponse;
  } catch {
    return null;
  }
}

export type ReportLine = {
  canonical_form: string;
  amount: string;
  account_number?: string;
  kind?: string;
};

export type ProfitAndLoss = {
  period: string;
  start_date: string;
  end_date: string;
  entity_label: string;
  entity_id: string;
  currency: string;
  revenue: { lines: ReportLine[]; total: string };
  expenses: { lines: ReportLine[]; total: string };
  net_income: string;
};

export type BalanceSheet = {
  as_of: string;
  entity_label: string;
  entity_id: string;
  currency: string;
  assets: { lines: ReportLine[]; total: string };
  liabilities: { lines: ReportLine[]; total: string };
  equity: { lines: ReportLine[]; total: string };
  balanced: boolean;
  drift: string;
};

export type CashFlowLine = {
  cash_account: string;
  counter_account: string;
  amount: string;
};

export type CashFlow = {
  period: string;
  start_date: string;
  end_date: string;
  entity_label: string;
  entity_id: string;
  currency: string;
  operating: { lines: CashFlowLine[]; total: string };
  investing: { lines: CashFlowLine[]; total: string };
  financing: { lines: CashFlowLine[]; total: string };
  net_change_in_cash: string;
};

async function fetchReport<T>(
  workspaceId: string,
  path: string,
  params: URLSearchParams
): Promise<T | null> {
  const session = await auth();
  if (!session?.accessToken) return null;
  try {
    const res = await fetch(
      `${INTERNAL_BASE}/api/workspaces/${workspaceId}/reports/${path}?${params}`,
      {
        headers: {
          Authorization: `Bearer ${session.accessToken}`,
          Accept: "application/json"
        },
        cache: "no-store"
      }
    );
    if (!res.ok) return null;
    return (await res.json()) as T;
  } catch {
    return null;
  }
}

export async function getProfitAndLoss(
  workspaceId: string,
  period: string = "ytd",
  entityId?: string
): Promise<ProfitAndLoss | null> {
  const params = new URLSearchParams({ period });
  if (entityId) params.set("entity_id", entityId);
  return fetchReport<ProfitAndLoss>(workspaceId, "pl", params);
}

export async function getBalanceSheet(
  workspaceId: string,
  asOf?: string,
  entityId?: string
): Promise<BalanceSheet | null> {
  const params = new URLSearchParams();
  if (asOf) params.set("as_of", asOf);
  if (entityId) params.set("entity_id", entityId);
  return fetchReport<BalanceSheet>(workspaceId, "balance-sheet", params);
}

export async function getCashFlow(
  workspaceId: string,
  period: string = "ytd",
  entityId?: string
): Promise<CashFlow | null> {
  const params = new URLSearchParams({ period });
  if (entityId) params.set("entity_id", entityId);
  return fetchReport<CashFlow>(workspaceId, "cash-flow", params);
}

export type Ten99NECPayee = {
  payee: string;
  total_amount: string;
  posting_count: number;
  requires_human_review: boolean;
};

export type Ten99NEC = {
  year: string;
  entity_label: string;
  entity_id: string;
  currency: string;
  threshold: string;
  /** GL account numbers treated as contract-labor (NEC basis), e.g. ["6300"]. */
  basis_accounts: string[];
  payees: Ten99NECPayee[];
  total_payees: number;
  total_paid: string;
  note: string;
};

export async function get1099NEC(
  workspaceId: string,
  year: string,
  entityId?: string
): Promise<Ten99NEC | null> {
  const params = new URLSearchParams({ year });
  if (entityId) params.set("entity_id", entityId);
  return fetchReport<Ten99NEC>(workspaceId, "1099-nec", params);
}

export function shortHash(hex: string): string {
  if (!hex) return "";
  return hex.slice(0, 8) + "…" + hex.slice(-4);
}

export function formatAmount(value: string | number, currency = "USD"): string {
  const n = typeof value === "string" ? parseFloat(value) : value;
  if (Number.isNaN(n)) return "—";
  const sign = n < 0 ? "-" : "";
  const abs = Math.abs(n);
  return `${sign}$${abs.toLocaleString("en-US", {
    minimumFractionDigits: 2,
    maximumFractionDigits: 2
  })}`;
}
