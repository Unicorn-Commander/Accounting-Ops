import Link from "next/link";
import { AlertTriangle, Download } from "lucide-react";
import { AppShell } from "@/components/app-shell";
import { EntitySelector } from "@/components/entity-selector";
import { Card, PageShell, SectionHeader } from "@/components/primitives";
import { getActiveContext } from "@/lib/active-context";
import {
  formatAmount,
  getBalanceSheet,
  getCashFlow,
  getProfitAndLoss
} from "@/lib/journal";
import { cn } from "@/lib/cn";

type SearchParams = {
  period?: string;
  view?: "pl" | "bs" | "cf";
  as_of?: string;
};

const PERIODS = [
  { v: "mtd", label: "MTD" },
  { v: "qtd", label: "QTD" },
  { v: "ytd", label: "YTD" },
  { v: "2026-Q1", label: "Q1" },
  { v: "2026-Q2", label: "Q2" },
  { v: "2026", label: "2026" },
  { v: "2025", label: "2025" }
];

export default async function ReportsPage(props: {
  searchParams: Promise<SearchParams>;
}) {
  const sp = await props.searchParams;
  const period = sp.period ?? "ytd";
  const view = sp.view ?? "pl";
  const ctx = await getActiveContext();
  const workspace = ctx.activeWorkspace;
  const entityFilter = ctx.isConsolidated ? "consolidated" : ctx.activeEntity?.id;
  const canConsolidate = ctx.activeWorkspace
    ? ["owner", "admin"].includes(ctx.activeWorkspace.role)
    : false;

  const [pl, bs, cf] = workspace
    ? await Promise.all([
        view === "pl" ? getProfitAndLoss(workspace.id, period, entityFilter) : null,
        view === "bs" ? getBalanceSheet(workspace.id, sp.as_of, entityFilter) : null,
        view === "cf" ? getCashFlow(workspace.id, period, entityFilter) : null
      ])
    : [null, null, null];

  return (
    <AppShell>
      <PageShell>
        <SectionHeader
          title="Reports"
          subtitle="Live financial statements pulled from the journal. Hash-chain audited."
          action={
            <EntitySelector
              entities={ctx.entities}
              activeEntityId={ctx.isConsolidated ? "consolidated" : ctx.activeEntity?.id}
              canConsolidate={canConsolidate}
              size="sm"
            />
          }
        />

        <div className="mb-6 flex flex-wrap items-center gap-2">
          {(["pl", "bs", "cf"] as const).map((v) => {
            const label = v === "pl" ? "Profit & Loss" : v === "bs" ? "Balance Sheet" : "Cash Flow";
            return (
              <Link
                key={v}
                href={`/reports?view=${v}&period=${period}`}
                className={cn(
                  "rounded-md border px-3 py-1.5 text-xs font-medium",
                  view === v
                    ? "border-brand-500 bg-brand-500/10 text-brand-300"
                    : "border-default bg-surface text-ink-secondary hover:bg-elevated"
                )}
              >
                {label}
              </Link>
            );
          })}
          <span className="mx-2 text-ink-tertiary">·</span>
          {view !== "bs" &&
            PERIODS.map((p) => (
              <Link
                key={p.v}
                href={`/reports?view=${view}&period=${p.v}`}
                className={cn(
                  "rounded-md border px-2.5 py-1 text-[11px]",
                  period === p.v
                    ? "border-brand-500 bg-brand-500/10 text-brand-300"
                    : "border-default bg-surface text-ink-tertiary hover:bg-elevated"
                )}
              >
                {p.label}
              </Link>
            ))}
          <span className="ml-auto">
            <Link
              href={`/cpa-export`}
              className="inline-flex items-center gap-1 rounded-md border border-default bg-surface px-3 py-1.5 text-xs text-ink-secondary hover:bg-elevated"
            >
              <Download size={11} /> Export PDF/CSV
            </Link>
          </span>
        </div>

        {view === "pl" && pl && (
          <PLView data={pl} />
        )}
        {view === "bs" && bs && (
          <BSView data={bs} />
        )}
        {view === "cf" && cf && (
          <CFView data={cf} />
        )}

        {(!pl && view === "pl") || (!bs && view === "bs") || (!cf && view === "cf") ? (
          <Card>
            <p className="text-sm text-ink-tertiary">Loading report…</p>
          </Card>
        ) : null}
      </PageShell>
    </AppShell>
  );
}

function PLView({ data }: { data: NonNullable<Awaited<ReturnType<typeof getProfitAndLoss>>> }) {
  return (
    <div>
      <p className="mb-4 text-xs text-ink-tertiary">
        {data.entity_label} · {data.start_date} → {data.end_date}
      </p>
      <Card padded={false}>
        <div className="border-b border-subtle px-5 py-2.5 text-[10px] font-medium uppercase tracking-wider text-credit">
          Revenue
        </div>
        {data.revenue.lines.length === 0 ? (
          <p className="px-5 py-3 text-xs text-ink-tertiary">No revenue in this period.</p>
        ) : (
          <table className="w-full text-sm">
            <tbody>
              {data.revenue.lines.map((l) => (
                <tr key={l.canonical_form} className="border-b border-subtle/40 last:border-b-0">
                  <td className="px-5 py-2 font-mono text-xs">{l.canonical_form}</td>
                  <td className="px-5 py-2 text-right font-mono tabular-nums text-credit">
                    {formatAmount(l.amount)}
                  </td>
                </tr>
              ))}
            </tbody>
            <tfoot>
              <tr className="border-t-2 border-credit/30 bg-credit/[0.04]">
                <td className="px-5 py-2 text-xs font-semibold uppercase tracking-wider">Total revenue</td>
                <td className="px-5 py-2 text-right font-mono tabular-nums font-semibold text-credit">
                  {formatAmount(data.revenue.total)}
                </td>
              </tr>
            </tfoot>
          </table>
        )}

        <div className="border-b border-t border-subtle px-5 py-2.5 text-[10px] font-medium uppercase tracking-wider text-debit">
          Expenses
        </div>
        {data.expenses.lines.length === 0 ? (
          <p className="px-5 py-3 text-xs text-ink-tertiary">No expenses in this period.</p>
        ) : (
          <table className="w-full text-sm">
            <tbody>
              {data.expenses.lines.map((l) => (
                <tr key={l.canonical_form} className="border-b border-subtle/40 last:border-b-0">
                  <td className="px-5 py-2 font-mono text-xs">{l.canonical_form}</td>
                  <td className="px-5 py-2 text-right font-mono tabular-nums text-debit">
                    -{formatAmount(l.amount)}
                  </td>
                </tr>
              ))}
            </tbody>
            <tfoot>
              <tr className="border-t-2 border-debit/30 bg-debit/[0.04]">
                <td className="px-5 py-2 text-xs font-semibold uppercase tracking-wider">Total expenses</td>
                <td className="px-5 py-2 text-right font-mono tabular-nums font-semibold text-debit">
                  -{formatAmount(data.expenses.total)}
                </td>
              </tr>
            </tfoot>
          </table>
        )}

        <div className="border-t-2 border-brand-500/40 bg-brand-500/[0.05] px-5 py-3">
          <div className="flex items-center justify-between">
            <span className="text-sm font-semibold uppercase tracking-wider">Net income</span>
            <span
              className={cn(
                "font-mono text-lg tabular-nums font-bold",
                parseFloat(data.net_income) >= 0 ? "text-credit" : "text-debit"
              )}
            >
              {formatAmount(data.net_income)}
            </span>
          </div>
        </div>
      </Card>
    </div>
  );
}

function BSView({ data }: { data: NonNullable<Awaited<ReturnType<typeof getBalanceSheet>>> }) {
  return (
    <div>
      <p className="mb-4 text-xs text-ink-tertiary">
        {data.entity_label} · as of {data.as_of}
      </p>
      <div className="grid grid-cols-1 gap-4 md:grid-cols-2">
        <Card padded={false}>
          <div className="border-b border-subtle px-5 py-2.5 text-[10px] font-medium uppercase tracking-wider text-credit">
            Assets
          </div>
          {data.assets.lines.length === 0 ? (
            <p className="px-5 py-3 text-xs text-ink-tertiary">No assets recorded yet.</p>
          ) : (
            <table className="w-full text-sm">
              <tbody>
                {data.assets.lines.map((l) => (
                  <tr key={l.canonical_form} className="border-b border-subtle/40 last:border-b-0">
                    <td className="px-5 py-2 font-mono text-xs">{l.canonical_form}</td>
                    <td className="px-5 py-2 text-right font-mono tabular-nums">{formatAmount(l.amount)}</td>
                  </tr>
                ))}
              </tbody>
              <tfoot>
                <tr className="border-t-2 border-credit/30 bg-credit/[0.04]">
                  <td className="px-5 py-2 text-xs font-semibold uppercase tracking-wider">Total assets</td>
                  <td className="px-5 py-2 text-right font-mono tabular-nums font-semibold">
                    {formatAmount(data.assets.total)}
                  </td>
                </tr>
              </tfoot>
            </table>
          )}
        </Card>

        <Card padded={false}>
          <div className="border-b border-subtle px-5 py-2.5 text-[10px] font-medium uppercase tracking-wider text-debit">
            Liabilities
          </div>
          {data.liabilities.lines.length === 0 ? (
            <p className="px-5 py-3 text-xs text-ink-tertiary">No liabilities recorded yet.</p>
          ) : (
            <table className="w-full text-sm">
              <tbody>
                {data.liabilities.lines.map((l) => (
                  <tr key={l.canonical_form} className="border-b border-subtle/40 last:border-b-0">
                    <td className="px-5 py-2 font-mono text-xs">{l.canonical_form}</td>
                    <td className="px-5 py-2 text-right font-mono tabular-nums">{formatAmount(l.amount)}</td>
                  </tr>
                ))}
              </tbody>
              <tfoot>
                <tr className="border-t-2 border-debit/30 bg-debit/[0.04]">
                  <td className="px-5 py-2 text-xs font-semibold uppercase tracking-wider">Total liabilities</td>
                  <td className="px-5 py-2 text-right font-mono tabular-nums font-semibold">
                    {formatAmount(data.liabilities.total)}
                  </td>
                </tr>
              </tfoot>
            </table>
          )}

          <div className="border-t border-subtle px-5 py-2.5 text-[10px] font-medium uppercase tracking-wider text-brand-300">
            Equity
          </div>
          <table className="w-full text-sm">
            <tbody>
              {data.equity.lines.map((l) => (
                <tr key={l.canonical_form} className="border-b border-subtle/40 last:border-b-0">
                  <td className="px-5 py-2 font-mono text-xs">{l.canonical_form}</td>
                  <td className="px-5 py-2 text-right font-mono tabular-nums">{formatAmount(l.amount)}</td>
                </tr>
              ))}
            </tbody>
            <tfoot>
              <tr className="border-t-2 border-brand-500/30 bg-brand-500/[0.04]">
                <td className="px-5 py-2 text-xs font-semibold uppercase tracking-wider">Total equity</td>
                <td className="px-5 py-2 text-right font-mono tabular-nums font-semibold">
                  {formatAmount(data.equity.total)}
                </td>
              </tr>
            </tfoot>
          </table>
        </Card>
      </div>

      <Card className="mt-4">
        <div className="flex items-center justify-between">
          <span className="text-sm font-medium">
            Assets = Liabilities + Equity check
          </span>
          {data.balanced ? (
            <span className="font-mono text-sm text-credit">balanced ✓</span>
          ) : (
            <span className="inline-flex items-center gap-1.5 font-mono text-sm text-pending">
              <AlertTriangle size={13} /> drift {formatAmount(data.drift)}
            </span>
          )}
        </div>
      </Card>
    </div>
  );
}

function CFView({ data }: { data: NonNullable<Awaited<ReturnType<typeof getCashFlow>>> }) {
  const sections: { title: string; data: typeof data.operating }[] = [
    { title: "Operating activities", data: data.operating },
    { title: "Investing activities", data: data.investing },
    { title: "Financing activities", data: data.financing }
  ];
  return (
    <div>
      <p className="mb-4 text-xs text-ink-tertiary">
        {data.entity_label} · {data.start_date} → {data.end_date}
      </p>
      <Card padded={false}>
        {sections.map((s) => (
          <div key={s.title}>
            <div className="border-b border-subtle px-5 py-2.5 text-[10px] font-medium uppercase tracking-wider text-ink-secondary">
              {s.title}
            </div>
            {s.data.lines.length === 0 ? (
              <p className="px-5 py-2 text-xs text-ink-tertiary">No activity.</p>
            ) : (
              <table className="w-full text-sm">
                <tbody>
                  {s.data.lines.map((l, i) => (
                    <tr key={`${s.title}-${i}`} className="border-b border-subtle/40 last:border-b-0">
                      <td className="px-5 py-2 font-mono text-xs">
                        {l.cash_account}
                        <span className="text-ink-tertiary"> ← </span>
                        {l.counter_account}
                      </td>
                      <td
                        className={cn(
                          "px-5 py-2 text-right font-mono tabular-nums",
                          parseFloat(l.amount) >= 0 ? "text-credit" : "text-debit"
                        )}
                      >
                        {formatAmount(l.amount)}
                      </td>
                    </tr>
                  ))}
                </tbody>
                <tfoot>
                  <tr className="bg-elevated">
                    <td className="px-5 py-2 text-[10px] font-semibold uppercase tracking-wider">Subtotal</td>
                    <td className="px-5 py-2 text-right font-mono tabular-nums font-semibold">
                      {formatAmount(s.data.total)}
                    </td>
                  </tr>
                </tfoot>
              </table>
            )}
          </div>
        ))}
        <div className="border-t-2 border-brand-500/40 bg-brand-500/[0.05] px-5 py-3">
          <div className="flex items-center justify-between">
            <span className="text-sm font-semibold uppercase tracking-wider">Net change in cash</span>
            <span
              className={cn(
                "font-mono text-lg tabular-nums font-bold",
                parseFloat(data.net_change_in_cash) >= 0 ? "text-credit" : "text-debit"
              )}
            >
              {formatAmount(data.net_change_in_cash)}
            </span>
          </div>
        </div>
      </Card>
    </div>
  );
}
