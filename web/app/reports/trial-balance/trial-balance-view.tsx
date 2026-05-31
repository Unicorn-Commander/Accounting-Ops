"use client";

import { useRouter } from "next/navigation";
import { Fragment, type FormEvent } from "react";
import { Check, RefreshCw, X } from "lucide-react";
import Link from "next/link";
import { Card, EmptyState, ErrorState } from "@/components/primitives";
import { cn } from "@/lib/cn";
import {
  centsAbsDiff,
  centsToDollars,
  type TrialBalance,
  type TrialBalanceAccount,
} from "@/lib/reports-trial-balance";

type AccountGroup = "asset" | "liability" | "equity" | "revenue" | "expense";

const GROUP_LABELS: Record<AccountGroup, string> = {
  asset: "Assets",
  liability: "Liabilities",
  equity: "Equity",
  revenue: "Revenue",
  expense: "Expenses",
};

const GROUP_ORDER: AccountGroup[] = [
  "asset",
  "liability",
  "equity",
  "revenue",
  "expense",
];

function groupFor(accountType: string): AccountGroup {
  if (accountType === "asset") return "asset";
  if (accountType === "liability") return "liability";
  if (accountType === "equity") return "equity";
  if (accountType === "revenue" || accountType === "other_income") return "revenue";
  return "expense";
}

function addCents(a: string, b: string): string {
  return (BigInt(a || "0") + BigInt(b || "0")).toString();
}

function subtotal(rows: TrialBalanceAccount[], key: keyof TrialBalanceAccount): string {
  return rows.reduce((sum, row) => addCents(sum, row[key]), "0");
}

export function TrialBalanceView({
  asOf,
  data,
}: {
  asOf: string;
  data: TrialBalance | null;
}) {
  const router = useRouter();
  const rows = [...(data?.accounts ?? [])].sort((a, b) =>
    a.account_number.localeCompare(b.account_number, "en-US", { numeric: true })
  );
  const grouped = GROUP_ORDER.map((group) => ({
    group,
    rows: rows.filter((row) => groupFor(row.account_type) === group),
  })).filter((group) => group.rows.length > 0);
  const drift = data
    ? centsAbsDiff(data.grand_total_debit, data.grand_total_credit)
    : "0";

  function refresh(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const formData = new FormData(event.currentTarget);
    const nextAsOf = String(formData.get("as_of") ?? asOf);
    router.push(`/reports/trial-balance?as_of=${encodeURIComponent(nextAsOf)}`);
    router.refresh();
  }

  return (
    <div className="space-y-5">
      <Card>
        <form onSubmit={refresh} className="flex flex-wrap items-end gap-3">
          <label className="grid gap-1.5 text-xs text-ink-secondary">
            <span className="font-medium uppercase tracking-wider text-ink-tertiary">
              As of
            </span>
            <input
              type="date"
              name="as_of"
              defaultValue={asOf}
              required
              className="h-9 rounded-md border border-default bg-canvas px-3 text-sm text-ink-primary outline-none focus:border-brand-500"
            />
          </label>
          <button
            type="submit"
            className="inline-flex h-9 items-center gap-1.5 rounded-md border border-default bg-surface px-3 text-sm text-ink-secondary hover:bg-elevated hover:text-ink-primary"
          >
            <RefreshCw size={14} />
            Refresh
          </button>
          {data?.entity_label ? (
            <p className="ml-auto text-xs text-ink-tertiary">
              {data.entity_label} · {data.as_of}
            </p>
          ) : null}
        </form>
      </Card>

      {data ? (
        <div
          className={cn(
            "rounded-lg border px-5 py-4",
            data.balanced
              ? "border-credit/30 bg-credit/[0.06] text-credit"
              : "border-debit/30 bg-debit/[0.06] text-debit"
          )}
        >
          <div className="flex flex-wrap items-center justify-between gap-3">
            <div className="inline-flex items-center gap-2 text-lg font-semibold">
              {data.balanced ? <Check size={20} /> : <X size={20} />}
              {data.balanced
                ? "Balanced"
                : `Out of balance by ${centsToDollars(drift)}`}
            </div>
            <div className="font-mono text-xs tabular-nums">
              Debits {centsToDollars(data.grand_total_debit)} · Credits{" "}
              {centsToDollars(data.grand_total_credit)}
            </div>
          </div>
        </div>
      ) : null}

      {!data ? (
        // H10: surface user-safe ErrorState instead of a bare card.
        <ErrorState
          title="Unable to load trial balance."
          description="The reporting service did not return data for this period. Try the Refresh button, or check connectivity."
        />
      ) : rows.length === 0 ? (
        // H9: "no posted entries yet" empty state, linking to /journal where
        // entries actually live.
        <EmptyState
          variant="no-data"
          title={`No posted entries as of ${asOf}.`}
          description="The trial balance is calculated from posted journal entries. Once entries land, the running totals show here."
          primaryAction={
            <Link
              href="/journal"
              className="inline-flex items-center gap-1.5 rounded-md bg-brand-500 px-3 py-1.5 text-xs font-medium text-white hover:bg-brand-600"
            >
              Open journal
            </Link>
          }
        />
      ) : (
        <Card padded={false}>
          <div className="overflow-x-auto">
            <table className="w-full min-w-[760px] text-sm">
              <thead>
                <tr className="border-b border-subtle text-left text-[10px] uppercase tracking-wider text-ink-tertiary">
                  <th className="px-5 py-3 font-medium">Account</th>
                  <th className="px-5 py-3 font-medium">Type</th>
                  <th className="px-5 py-3 text-right font-medium">Debit</th>
                  <th className="px-5 py-3 text-right font-medium">Credit</th>
                  <th className="px-5 py-3 text-right font-medium">Net</th>
                </tr>
              </thead>
              <tbody>
                {grouped.map(({ group, rows: groupRows }) => (
                  <Fragment key={group}>
                    <tr className="bg-white/[0.02]">
                      <td
                        colSpan={5}
                        className="border-b border-subtle px-5 py-2 text-[10px] font-medium uppercase tracking-wider text-ink-tertiary"
                      >
                        {GROUP_LABELS[group]}
                      </td>
                    </tr>
                    {groupRows.map((row) => (
                      <tr
                        key={row.canonical_form}
                        className="border-b border-subtle/40 last:border-b-0"
                      >
                        <td className="px-5 py-2.5 font-mono text-xs">
                          {row.canonical_form}
                        </td>
                        <td className="px-5 py-2.5 text-xs text-ink-secondary">
                          {row.account_type}
                        </td>
                        <td className="px-5 py-2.5 text-right font-mono tabular-nums">
                          {centsToDollars(row.total_debit)}
                        </td>
                        <td className="px-5 py-2.5 text-right font-mono tabular-nums">
                          {centsToDollars(row.total_credit)}
                        </td>
                        <td
                          className={cn(
                            "px-5 py-2.5 text-right font-mono tabular-nums",
                            row.net_balance.startsWith("-")
                              ? "text-debit"
                              : "text-credit"
                          )}
                        >
                          {centsToDollars(row.net_balance)}
                        </td>
                      </tr>
                    ))}
                    <tr className="bg-white/[0.025]">
                      <td className="px-5 py-2 text-xs font-semibold uppercase tracking-wider">
                        {GROUP_LABELS[group]} subtotal
                      </td>
                      <td />
                      <td className="px-5 py-2 text-right font-mono text-xs font-semibold tabular-nums">
                        {centsToDollars(subtotal(groupRows, "total_debit"))}
                      </td>
                      <td className="px-5 py-2 text-right font-mono text-xs font-semibold tabular-nums">
                        {centsToDollars(subtotal(groupRows, "total_credit"))}
                      </td>
                      <td className="px-5 py-2 text-right font-mono text-xs font-semibold tabular-nums">
                        {centsToDollars(subtotal(groupRows, "net_balance"))}
                      </td>
                    </tr>
                  </Fragment>
                ))}
              </tbody>
              <tfoot>
                <tr className="border-t-2 border-brand-500/40 bg-brand-500/[0.05]">
                  <td className="px-5 py-3 text-xs font-semibold uppercase tracking-wider">
                    Grand total
                  </td>
                  <td />
                  <td className="px-5 py-3 text-right font-mono font-semibold tabular-nums">
                    {centsToDollars(data.grand_total_debit)}
                  </td>
                  <td className="px-5 py-3 text-right font-mono font-semibold tabular-nums">
                    {centsToDollars(data.grand_total_credit)}
                  </td>
                  <td className="px-5 py-3 text-right font-mono font-semibold tabular-nums">
                    {centsToDollars(
                      addCents(data.grand_total_debit, `-${data.grand_total_credit}`)
                    )}
                  </td>
                </tr>
              </tfoot>
            </table>
          </div>
        </Card>
      )}
    </div>
  );
}
