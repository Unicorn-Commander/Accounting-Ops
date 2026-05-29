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
  getProfitAndLoss,
  get1099NEC
} from "@/lib/journal";
import { cn } from "@/lib/cn";

type SearchParams = {
  period?: string;
  view?: "pl" | "bs" | "cf" | "1099-nec";
  as_of?: string;
  year?: string;
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

const YEARS = ["2026", "2025", "2024", "2023"];

export default async function ReportsPage(props: {
  searchParams: Promise<SearchParams>;
}) {
  const sp = await props.searchParams;
  const period = sp.period ?? "ytd";
  const view = sp.view ?? "pl";
  const year = sp.year ?? "2026";
  const ctx = await getActiveContext();
  const workspace = ctx.activeWorkspace;
  const entityFilter = ctx.isConsolidated ? "consolidated" : ctx.activeEntity?.id;
  const canConsolidate = ctx.activeWorkspace
    ? ["owner", "admin"].includes(ctx.activeWorkspace.role)
    : false;

  const [pl, bs, cf, ten99] = workspace
    ? await Promise.all([
        view === "pl" ? getProfitAndLoss(workspace.id, period, entityFilter) : null,
        view === "bs" ? getBalanceSheet(workspace.id, sp.as_of, entityFilter) : null,
        view === "cf" ? getCashFlow(workspace.id, period, entityFilter) : null,
        view === "1099-nec" ? get1099NEC(workspace.id, year, entityFilter) : null
      ])
    : [null, null, null, null];

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
          {(["pl", "bs", "cf", "1099-nec"] as const).map((v) => {
            const label = v === "pl" ? "Profit & Loss" : v === "bs" ? "Balance Sheet" : v === "cf" ? "Cash Flow" : "1099-NEC";
            return (
              <Link
                key={v}
                href={`/reports?view=${v}${view !== "1099-nec" && v !== "1099-nec" ? `&period=${period}` : v === "1099-nec" ? `&year=${year}` : ``}`}
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
          {view !== "bs" && view !== "1099-nec" &&
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
          {view === "1099-nec" &&
            YEARS.map((y) => (
              <Link
                key={y}
                href={`/reports?view=1099-nec&year=${y}`}
                className={cn(
                  "rounded-md border px-2.5 py-1 text-[11px]",
                  year === y
                    ? "border-brand-500 bg-brand-500/10 text-brand-300"
                    : "border-default bg-surface text-ink-tertiary hover:bg-elevated"
                )}
              >
                {y}
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
        {view === "1099-nec" && ten99 && (
          <Ten99NECView data={ten99} />
        )}

        {(!pl && view === "pl") || (!bs && view === "bs") || (!cf && view === "cf") || (!ten99 && view === "1099-nec") ? (
          <Card>
            <div className="py-8 text-center">
              <p className="text-sm font-medium text-ink-secondary">
                No data to report yet for this period.
              </p>
              <p className="mx-auto mt-1 max-w-md text-xs text-ink-tertiary">
                Reports pull live from the journal&apos;s hash-chained entries.
                Once you post entries (manually or via the approval queue) for
                the selected entity and date range, they&apos;ll appear here.
              </p>
              <Link
                href="/approvals"
                className="mt-4 inline-flex items-center gap-1.5 rounded-md border border-default bg-elevated px-3 py-1.5 text-xs text-ink-secondary hover:text-ink-primary"
              >
                Review pending approvals
              </Link>
            </div>
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
          <div className="overflow-x-auto"><table className="w-full min-w-[480px] text-sm">
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
          </table></div>
        )}

        <div className="border-b border-t border-subtle px-5 py-2.5 text-[10px] font-medium uppercase tracking-wider text-debit">
          Expenses
        </div>
        {data.expenses.lines.length === 0 ? (
          <p className="px-5 py-3 text-xs text-ink-tertiary">No expenses in this period.</p>
        ) : (
          <div className="overflow-x-auto"><table className="w-full min-w-[480px] text-sm">
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
          </table></div>
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
            <div className="overflow-x-auto"><table className="w-full min-w-[480px] text-sm">
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
            </table></div>
          )}
        </Card>

        <Card padded={false}>
          <div className="border-b border-subtle px-5 py-2.5 text-[10px] font-medium uppercase tracking-wider text-debit">
            Liabilities
          </div>
          {data.liabilities.lines.length === 0 ? (
            <p className="px-5 py-3 text-xs text-ink-tertiary">No liabilities recorded yet.</p>
          ) : (
            <div className="overflow-x-auto"><table className="w-full min-w-[480px] text-sm">
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
            </table></div>
          )}

          <div className="border-t border-subtle px-5 py-2.5 text-[10px] font-medium uppercase tracking-wider text-brand-300">
            Equity
          </div>
          <div className="overflow-x-auto"><table className="w-full min-w-[480px] text-sm">
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
          </table></div>
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
              <div className="overflow-x-auto"><table className="w-full min-w-[480px] text-sm">
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
              </table></div>
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

function Ten99NECView({ data }: { data: NonNullable<Awaited<ReturnType<typeof get1099NEC>>> }) {
  const hasPayees = data.payees.length > 0;
  return (
    <div>
      <p className="mb-4 text-xs text-ink-tertiary">
        {data.entity_label} · Calendar Year {data.year} · Threshold: {formatAmount(data.threshold)}
        {data.basis_accounts?.length ? ` · Contractor acct ${data.basis_accounts.join(", ")}` : ""}
      </p>

      <Card>
        <div className="mb-3 flex items-center justify-between">
          <span className="text-xs font-semibold uppercase tracking-wider text-ink-secondary">
            {data.total_payees} payee{data.total_payees !== 1 ? "s" : ""} ≥ {formatAmount(data.threshold)}
          </span>
          <span className="font-mono text-sm font-bold text-credit">
            {formatAmount(data.total_paid)}
          </span>
        </div>

        {hasPayees ? (
          <div className="overflow-x-auto">
            <table className="w-full min-w-[480px] text-sm">
              <thead>
                <tr className="border-b border-subtle text-[10px] font-medium uppercase tracking-wider text-ink-tertiary">
                  <th className="px-5 py-2 text-left">Payee</th>
                  <th className="px-5 py-2 text-right">Total Amount</th>
                  <th className="px-5 py-2 text-center">Postings</th>
                  <th className="px-5 py-2 text-center">Review</th>
                </tr>
              </thead>
              <tbody>
                {data.payees.map((p, i) => (
                  <tr key={`${p.payee}-${i}`} className="border-b border-subtle/40 last:border-b-0">
                    <td className="px-5 py-2 font-mono text-xs">
                      {p.payee}
                      {p.requires_human_review && (
                        <span className="ml-2 inline-flex items-center gap-1 rounded bg-pending/10 px-1.5 py-0.5 text-[10px] font-medium text-pending">
                          <AlertTriangle size={10} /> Processor
                        </span>
                      )}
                    </td>
                    <td className="px-5 py-2 text-right font-mono tabular-nums font-semibold">
                      {formatAmount(p.total_amount)}
                    </td>
                    <td className="px-5 py-2 text-center font-mono tabular-nums text-ink-tertiary">
                      {p.posting_count}
                    </td>
                    <td className="px-5 py-2 text-center">
                      {p.requires_human_review ? (
                        <span className="text-[10px] text-pending">Needs review</span>
                      ) : (
                        <span className="text-[10px] text-ink-tertiary">—</span>
                      )}
                    </td>
                  </tr>
                ))}
              </tbody>
              <tfoot>
                <tr className="border-t-2 border-credit/30 bg-credit/[0.04]">
                  <td className="px-5 py-2 text-xs font-semibold uppercase tracking-wider">Total</td>
                  <td className="px-5 py-2 text-right font-mono tabular-nums font-semibold text-credit">
                    {formatAmount(data.total_paid)}
                  </td>
                  <td className="px-5 py-2"></td>
                  <td className="px-5 py-2"></td>
                </tr>
              </tfoot>
            </table>
          </div>
        ) : (
          <p className="px-5 py-3 text-xs text-ink-tertiary">
            No payees reached the $600 threshold for {data.year}.
          </p>
        )}

        <div className="mt-4 border-t border-subtle px-5 py-3">
          <p className="text-[11px] text-ink-tertiary">
            <strong>Note:</strong> This report does not auto-determine corporate status or
            payment-method exclusions. Payees flagged "Processor" may have been paid via
            credit-card processors (1099-K territory). Verify before filing.
          </p>
        </div>
      </Card>
    </div>
  );
}
