// Real-time transaction fetch via the FastAPI backend.

import { auth } from "@/lib/auth";

const INTERNAL_BASE =
  process.env.INTERNAL_API_BASE_URL ??
  process.env.NEXT_PUBLIC_API_BASE_URL ??
  "http://app:8000";

export type LiveTransaction = {
  id: string;
  account_id: string;
  account_name: string;
  org_name: string;
  currency: string;
  posted_at: string | null;
  posted_ts: number | null;
  transacted_at: string | null;
  amount: string;
  payee: string | null;
  description: string;
  memo: string | null;
  is_pending: boolean;
};

export type TransactionsResponse = {
  configured: boolean;
  source?: string;
  days_window?: number;
  total_count?: number;
  transactions: LiveTransaction[];
  message?: string;
  errors?: string[];
};

export async function getRecentTransactions(
  workspaceId: string,
  days = 30,
  limit = 50
): Promise<TransactionsResponse | null> {
  const session = await auth();
  if (!session?.accessToken) return null;
  try {
    const res = await fetch(
      `${INTERNAL_BASE}/api/workspaces/${workspaceId}/recent-transactions?days=${days}&limit=${limit}`,
      {
        headers: {
          Authorization: `Bearer ${session.accessToken}`,
          Accept: "application/json"
        },
        cache: "no-store"
      }
    );
    if (!res.ok) return null;
    return (await res.json()) as TransactionsResponse;
  } catch {
    return null;
  }
}

export function shortDate(iso: string | null): string {
  if (!iso) return "—";
  const d = new Date(iso);
  if (Number.isNaN(d.valueOf())) return "—";
  return d.toLocaleDateString("en-US", { month: "short", day: "numeric" });
}
