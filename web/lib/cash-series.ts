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
