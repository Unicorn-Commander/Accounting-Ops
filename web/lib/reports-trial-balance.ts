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

// Normalize a money string to integer cents. Accepts EITHER a decimal-dollar
// string from the backend (Postgres NUMERIC, e.g. "51064.4800000000000000")
// OR an integer-cents string produced by our own addCents/subtotal helpers.
// The decimal point is the discriminator (the API always sends decimals; our
// intermediates never do), so BigInt() is only ever handed integers.
export function valueToCents(value: string): bigint {
  const s = (value ?? "0").trim();
  if (!s) return 0n;
  const negative = s.startsWith("-");
  const abs = s.replace(/^-/, "");
  let cents: bigint;
  if (abs.includes(".")) {
    const [intPart, fracRaw = ""] = abs.split(".");
    const frac = (fracRaw + "00").slice(0, 2); // first 2 decimals = cents
    cents = BigInt(intPart || "0") * 100n + BigInt(frac || "0");
  } else {
    cents = BigInt(abs || "0"); // already integer cents
  }
  return negative ? -cents : cents;
}

export function centsToDollars(value: string): string {
  const signed = valueToCents(value);
  const negative = signed < 0n;
  const cents = negative ? -signed : signed;
  const dollars = cents / 100n;
  const remainder = cents % 100n;
  return `${negative ? "-" : ""}$${dollars.toLocaleString("en-US")}.${remainder
    .toString()
    .padStart(2, "0")}`;
}

export function centsAbsDiff(a: string, b: string): string {
  const left = valueToCents(a);
  const right = valueToCents(b);
  const diff = left >= right ? left - right : right - left;
  return diff.toString();
}
