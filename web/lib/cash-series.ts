// REAL 90-day net-cash series, reconstructed from the actual transaction ledger.
//
// We start from the real current net-cash balance and walk BACKWARDS day by day,
// removing each day's net transaction delta to recover the historical end-of-day
// balance. Every point is therefore derived from real data — no synthetic noise.
//
// Limitation: we reconstruct from however many transactions the window returns
// (capped by the fetch limit). For very high-volume books that exceed the cap the
// earliest points drift slightly, but the curve is still real ledger movement —
// never a generated shape. When the backend ships a dedicated daily-balance
// endpoint, swap this for it.

import type { LiveTransaction } from "@/lib/transactions";
import { auth } from "@/lib/auth";

const INTERNAL_BASE =
  process.env.INTERNAL_API_BASE_URL ??
  process.env.NEXT_PUBLIC_API_BASE_URL ??
  "http://app:8000";

const DAYS = 90;

export type CashSeries = {
  series: number[];
  min: number;
  max: number;
  current: number;
  delta: number; // % change across the window, from real endpoints
  hasHistory: boolean; // false when there are no dated transactions to reconstruct from
  annotation?: { index: number; label: string; value: string };
};

function round2(n: number): number {
  return Math.round(n * 100) / 100;
}

export function buildRealCashSeries(
  currentNetCash: number,
  transactions: LiveTransaction[],
  days = DAYS
): CashSeries {
  // Net transaction delta per UTC calendar day.
  const byDay = new Map<string, number>();
  let dated = 0;
  for (const t of transactions) {
    const iso = t.posted_at ?? t.transacted_at;
    if (!iso) continue;
    const amt = parseFloat(t.amount);
    if (!Number.isFinite(amt)) continue;
    const day = iso.slice(0, 10);
    byDay.set(day, (byDay.get(day) ?? 0) + amt);
    dated++;
  }

  const series = new Array<number>(days);
  let bal = currentNetCash;
  const cursor = new Date();
  for (let i = days - 1; i >= 0; i--) {
    series[i] = round2(bal);
    const dayStr = cursor.toISOString().slice(0, 10);
    bal -= byDay.get(dayStr) ?? 0; // remove this day's net → prior day's ending balance
    cursor.setUTCDate(cursor.getUTCDate() - 1);
  }

  const min = Math.min(...series);
  const max = Math.max(...series);
  const first = series[0];
  const delta = first !== 0 ? ((series[days - 1] - first) / Math.abs(first)) * 100 : 0;

  return {
    series,
    min,
    max,
    current: currentNetCash,
    delta,
    hasHistory: dated > 0,
    annotation: undefined,
  };
}

// Backend GL daily-balance series — the uncapped replacement for the
// reconstruction above. The endpoint returns real end-of-day cash balances
// computed over the FULL posted journal (no transaction-fetch cap), so the
// earliest points no longer drift on high-volume books.
export type DailyBalanceResponse = {
  days: number;
  start_date: string;
  as_of: string;
  currency: string;
  opening: number;
  series: number[]; // GL ending cash per day, oldest → newest
  gl_current: number; // = series[last]; reconciles to journal cash as of today
  has_history: boolean;
  active_days: number;
  source: string;
};

export async function getDailyCashBalance(
  workspaceId: string,
  days = DAYS,
  entityId?: string
): Promise<DailyBalanceResponse | null> {
  const session = await auth();
  if (!session?.accessToken) return null;
  try {
    const params = new URLSearchParams({ days: String(days) });
    if (entityId) params.set("entity_id", entityId);
    const res = await fetch(
      `${INTERNAL_BASE}/api/workspaces/${workspaceId}/cash/daily-balance?${params.toString()}`,
      {
        headers: {
          Authorization: `Bearer ${session.accessToken}`,
          Accept: "application/json"
        },
        cache: "no-store"
      }
    );
    if (!res.ok) return null;
    return (await res.json()) as DailyBalanceResponse;
  } catch {
    return null;
  }
}

// Assemble a CashSeries from the backend GL series. The ribbon's headline
// `current` stays anchored to the live net-cash figure shown elsewhere on the
// dashboard (cashPosition.net_total): when a bank is connected we shift the GL
// shape by a constant so its last point equals that number — keeping the
// headline consistent while every movement is real ledger data. With no bank
// connected we report the journal cash directly.
export function buildCashSeriesFromDaily(
  daily: DailyBalanceResponse,
  currentNetCash: number,
  configured: boolean
): CashSeries {
  const anchorToBank = configured && Number.isFinite(currentNetCash);
  const offset = anchorToBank ? currentNetCash - daily.gl_current : 0;
  const series = daily.series.map((v) => round2(v + offset));
  const current = anchorToBank ? currentNetCash : daily.gl_current;

  const min = series.length ? Math.min(...series) : 0;
  const max = series.length ? Math.max(...series) : 0;
  const first = series.length ? series[0] : 0;
  const last = series.length ? series[series.length - 1] : 0;
  const delta = first !== 0 ? ((last - first) / Math.abs(first)) * 100 : 0;

  return {
    series,
    min,
    max,
    current: round2(current),
    delta,
    hasHistory: daily.has_history,
    annotation: undefined,
  };
}
