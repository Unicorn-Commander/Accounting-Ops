// Real-time cash position fetch. Server-side, hits the FastAPI backend with
// the caller's bearer token; backend fans out to SimpleFIN.

import { auth } from "@/lib/auth";

const INTERNAL_BASE =
  process.env.INTERNAL_API_BASE_URL ??
  process.env.NEXT_PUBLIC_API_BASE_URL ??
  "http://app:8000";

export type CashAccount = {
  id: string;
  name: string;
  org_name: string;
  currency: string;
  balance: string;
  available_balance: string | null;
  balance_as_of: number | null;
  last4: string | null;
};

export type CashPosition = {
  total_usd: string;
  currency: string;
  accounts: CashAccount[];
  configured: boolean;
  source?: string;
  message?: string;
  errors?: string[];
};

export async function getCashPosition(workspaceId: string): Promise<CashPosition | null> {
  const session = await auth();
  if (!session?.accessToken) return null;
  try {
    const res = await fetch(
      `${INTERNAL_BASE}/api/workspaces/${workspaceId}/cash-position`,
      {
        headers: {
          Authorization: `Bearer ${session.accessToken}`,
          Accept: "application/json"
        },
        cache: "no-store"
      }
    );
    if (!res.ok) return null;
    return (await res.json()) as CashPosition;
  } catch {
    return null;
  }
}

export function formatUSD(value: string | number): string {
  const n = typeof value === "string" ? parseFloat(value) : value;
  if (Number.isNaN(n)) return "—";
  const sign = n < 0 ? "-" : "";
  const abs = Math.abs(n);
  return `${sign}$${abs.toLocaleString("en-US", {
    minimumFractionDigits: 2,
    maximumFractionDigits: 2
  })}`;
}
