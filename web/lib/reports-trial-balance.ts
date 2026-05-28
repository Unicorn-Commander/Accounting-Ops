import { auth } from "@/lib/auth";

const INTERNAL_BASE =
  process.env.INTERNAL_API_BASE_URL ??
  process.env.NEXT_PUBLIC_API_BASE_URL ??
  "http://app:8000";

export type TrialBalanceAccount = {
  account_number: string;
  canonical_form: string;
  account_type: string;
  total_debit: string;
  total_credit: string;
  net_balance: string;
};

export type TrialBalance = {
  as_of: string;
  entity_label?: string;
  entity_id: string;
  currency?: string;
  accounts: TrialBalanceAccount[];
  grand_total_debit: string;
  grand_total_credit: string;
  balanced: boolean;
};

export type ClosedPeriod = {
  period_end: string;
  closed_at: string;
  closed_by_uc_uid: string;
  reason: string | null;
};

export type ClosedPeriodsResponse = {
  entity_id: string;
  closed_periods: ClosedPeriod[];
};

export type ClosePeriodResponse = {
  ok: boolean;
  entity_id: string;
  entity_display_name: string;
  closed_period: ClosedPeriod;
  entries_in_period: number;
  note: string;
};

async function fetchBackend<T>(
  path: string,
  init?: RequestInit
): Promise<T | null> {
  const session = await auth();
  if (!session?.accessToken) return null;

  const res = await fetch(`${INTERNAL_BASE}${path}`, {
    ...init,
    headers: {
      Authorization: `Bearer ${session.accessToken}`,
      Accept: "application/json",
      ...(init?.body ? { "Content-Type": "application/json" } : {}),
      ...init?.headers,
    },
    cache: "no-store",
  });

  if (!res.ok) return null;
  return (await res.json()) as T;
}

export async function getTrialBalance(
  workspaceId: string,
  asOf: string,
  entityId?: string
): Promise<TrialBalance | null> {
  const params = new URLSearchParams({ as_of: asOf });
  if (entityId) params.set("entity_id", entityId);
  return fetchBackend<TrialBalance>(
    `/api/workspaces/${workspaceId}/reports/trial-balance?${params}`
  );
}

export async function getClosedPeriods(
  workspaceId: string,
  entityId: string
): Promise<ClosedPeriodsResponse | null> {
  return fetchBackend<ClosedPeriodsResponse>(
    `/api/workspaces/${workspaceId}/entities/${entityId}/closed-periods`
  );
}

export async function closePeriod(
  workspaceId: string,
  entityId: string,
  body: { period_end: string; reason?: string | null }
): Promise<ClosePeriodResponse | null> {
  return fetchBackend<ClosePeriodResponse>(
    `/api/workspaces/${workspaceId}/entities/${entityId}/close-period`,
    {
      method: "POST",
      body: JSON.stringify(body),
    }
  );
}

export function centsToDollars(value: string): string {
  if (!value) return "$0.00";
  const negative = value.trim().startsWith("-");
  const digits = value.trim().replace(/^-/, "");
  const cents = BigInt(digits || "0");
  const dollars = cents / 100n;
  const remainder = cents % 100n;
  return `${negative ? "-" : ""}$${dollars.toLocaleString("en-US")}.${remainder
    .toString()
    .padStart(2, "0")}`;
}

export function centsAbsDiff(a: string, b: string): string {
  const left = BigInt(a || "0");
  const right = BigInt(b || "0");
  const diff = left >= right ? left - right : right - left;
  return diff.toString();
}
