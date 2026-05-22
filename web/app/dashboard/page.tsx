import Link from "next/link";
import { ArrowRight, ArrowUpRight, Wifi } from "lucide-react";
import { AppShell } from "@/components/app-shell";
import { EntitySelector } from "@/components/entity-selector";
import { Card, KpiCard, MaterialityBadge, PageShell, SectionHeader, StatusChip, ConfidenceDot } from "@/components/primitives";
import { getActiveContext } from "@/lib/active-context";
import { getCashPosition, formatUSD } from "@/lib/cash-position";
import { getRecentTransactions, shortDate, type LiveTransaction } from "@/lib/transactions";
import { cn } from "@/lib/cn";

const RECENT_TXNS = [
  { id: "tx-1", date: "May 21", payee: "Stripe payout (Listing-Ops)", category: "Revenue · Marketplace", amount: "+$12,408.22", tone: "credit", conf: 0.97, materiality: "M" as const, recon: "reconciled" as const },
  { id: "tx-2", date: "May 21", payee: "Anthropic", category: "SaaS · AI", amount: "-$200.00", tone: "debit", conf: 0.93, materiality: "L" as const, recon: "reconciled" as const },
  { id: "tx-3", date: "May 21", payee: "Hostinger", category: "Infrastructure", amount: "-$8.99", tone: "debit", conf: 0.95, materiality: "L" as const, recon: "reconciled" as const },
  { id: "tx-4", date: "May 20", payee: "Stripe payout (Majiks Studio)", category: "Revenue · Music", amount: "+$1,082.40", tone: "credit", conf: 0.99, materiality: "L" as const, recon: "reconciled" as const },
  { id: "tx-5", date: "May 20", payee: "Cisco Meraki", category: "Infrastructure", amount: "-$420.00", tone: "debit", conf: 0.71, materiality: "L" as const, recon: "needs_review" as const },
  { id: "tx-6", date: "May 19", payee: "Mercury · ACH transfer", category: "Inter-entity", amount: "+$5,000.00", tone: "credit", conf: 0.62, materiality: "M" as const, recon: "needs_review" as const },
  { id: "tx-7", date: "May 19", payee: "Postmark", category: "SaaS · Email", amount: "-$15.00", tone: "debit", conf: 0.98, materiality: "L" as const, recon: "reconciled" as const }
];

export default async function DashboardPage() {
  const ctx = await getActiveContext();
  const canConsolidate = ctx.activeWorkspace
    ? ["owner", "admin"].includes(ctx.activeWorkspace.role)
    : false;
  const cashPosition = ctx.activeWorkspace
    ? await getCashPosition(ctx.activeWorkspace.id)
    : null;
  const txnData = ctx.activeWorkspace
    ? await getRecentTransactions(ctx.activeWorkspace.id, 30, 12)
    : null;
  const liveTxns: LiveTransaction[] = txnData?.transactions ?? [];
  const subtitle = ctx.activeWorkspace
    ? `${ctx.activeWorkspace.display_name} · ${ctx.entities.length} ${ctx.entities.length === 1 ? "entity" : "entities"} · viewing ${ctx.isConsolidated ? "all entities (consolidated)" : ctx.activeEntity?.display_name ?? "—"}`
    : "Cash position, agent activity, and approvals across all your entities.";
  return (
    <AppShell>
      <PageShell>
        <SectionHeader
          title="Dashboard"
          subtitle={subtitle}
          action={
            <EntitySelector
              entities={ctx.entities}
              activeEntityId={ctx.isConsolidated ? "consolidated" : ctx.activeEntity?.id}
              canConsolidate={canConsolidate}
            />
          }
        />

        <div className="grid grid-cols-1 gap-4 md:grid-cols-2 lg:grid-cols-4">
          <KpiCard
            label="Cash position"
            value={cashPosition?.configured ? formatUSD(cashPosition.total_usd) : "$—"}
            delta={
              cashPosition?.configured
                ? `Live · ${cashPosition.accounts.length} accounts via SimpleFIN`
                : "Connect SimpleFIN to see live"
            }
            deltaTone={cashPosition?.configured ? "credit" : "ink"}
          />
          <KpiCard label="Net burn (30d)" value="$—" delta="Phase 1.6: requires Q-on-Q txn ingest" deltaTone="ink" />
          <KpiCard label="Runway" value="—" delta="Awaiting burn rate" deltaTone="ink" />
          <KpiCard label="Awaiting approval" value="0" delta="No agents wired to ledger yet" deltaTone="ink" />
        </div>

        <div className="mt-8 grid grid-cols-1 gap-4 lg:grid-cols-3">
          <div className="lg:col-span-2">
            <Card padded={false}>
              <div className="flex items-center justify-between border-b border-subtle px-6 py-4">
                <div>
                  <h2 className="text-base font-semibold">Recent transactions</h2>
                  <p className="mt-0.5 text-xs text-ink-tertiary">Last 7 days across connected accounts</p>
                </div>
                <Link
                  href="/transactions"
                  className="inline-flex items-center gap-1 text-xs text-ink-secondary hover:text-ink-primary"
                >
                  View all <ArrowRight size={13} />
                </Link>
              </div>
              {liveTxns.length > 0 ? (
                <table className="w-full text-sm">
                  <thead className="text-[10px] uppercase tracking-wider text-ink-tertiary">
                    <tr className="border-b border-subtle">
                      <th className="px-6 py-2.5 text-left font-medium">Date</th>
                      <th className="px-2 py-2.5 text-left font-medium">Payee · Account</th>
                      <th className="px-2 py-2.5 text-left font-medium">Status</th>
                      <th className="px-2 py-2.5 text-center font-medium">M</th>
                      <th className="px-2 py-2.5 text-center font-medium">Conf</th>
                      <th className="px-6 py-2.5 text-right font-medium">Amount</th>
                    </tr>
                  </thead>
                  <tbody>
                    {liveTxns.map((t) => {
                      const amt = parseFloat(t.amount);
                      const isCredit = amt >= 0;
                      const absAmt = Math.abs(amt);
                      const materiality: "L" | "M" | "H" =
                        absAmt < 100 ? "L" : absAmt < 1000 ? "M" : "H";
                      return (
                        <tr
                          key={t.id}
                          className="group cursor-pointer border-b border-subtle/60 last:border-b-0 hover:bg-white/[0.02]"
                        >
                          <td className="whitespace-nowrap px-6 py-3 font-mono text-xs text-ink-tertiary">
                            {shortDate(t.posted_at)}
                          </td>
                          <td className="px-2 py-3">
                            <div className="font-medium truncate max-w-[260px]" title={t.payee ?? t.description}>
                              {t.payee ?? t.description ?? "—"}
                            </div>
                            <div className="text-xs text-ink-tertiary truncate" title={`${t.org_name} · ${t.account_name}`}>
                              {t.org_name} · {t.account_name}
                            </div>
                          </td>
                          <td className="px-2 py-3">
                            {t.is_pending ? (
                              <StatusChip tone="pending">Pending</StatusChip>
                            ) : (
                              <StatusChip tone="ink">Posted</StatusChip>
                            )}
                          </td>
                          <td className="px-2 py-3 text-center">
                            <MaterialityBadge level={materiality} />
                          </td>
                          <td className="px-2 py-3 text-center">
                            <ConfidenceDot value={0} />
                          </td>
                          <td
                            className={cn(
                              "px-6 py-3 text-right font-mono tabular-nums",
                              isCredit ? "text-credit" : "text-ink-primary"
                            )}
                          >
                            {isCredit ? "+" : "-"}
                            {formatUSD(absAmt)}
                          </td>
                        </tr>
                      );
                    })}
                  </tbody>
                </table>
              ) : (
                <div className="px-6 py-8 text-center text-xs text-ink-tertiary">
                  {txnData?.configured
                    ? "No transactions in the last 30 days across connected accounts."
                    : "No live transaction feed yet."}{" "}
                  <Link
                    href="/connectors"
                    className="text-ink-secondary underline-offset-2 hover:text-ink-primary hover:underline"
                  >
                    Connect a bank
                  </Link>{" "}
                  to populate.
                </div>
              )}
            </Card>
          </div>

          <div className="space-y-4">
            <Card padded={false}>
              <div className="flex items-center justify-between border-b border-subtle px-5 py-4">
                <div>
                  <h2 className="text-base font-semibold">Cash by account</h2>
                  <p className="mt-0.5 text-xs text-ink-tertiary">
                    {cashPosition?.configured ? (
                      <span className="inline-flex items-center gap-1.5">
                        <Wifi size={11} className="text-credit" />
                        Live · SimpleFIN ·{" "}
                        {cashPosition.accounts[0]?.balance_as_of
                          ? new Date(cashPosition.accounts[0].balance_as_of * 1000).toLocaleString(
                              "en-US",
                              { dateStyle: "short", timeStyle: "short" }
                            )
                          : ""}
                      </span>
                    ) : (
                      "Connect a banking aggregator to see live balances"
                    )}
                  </p>
                </div>
                <Link
                  href="/connectors"
                  className="text-[11px] text-ink-secondary hover:text-ink-primary"
                >
                  Connectors →
                </Link>
              </div>
              <ul className="divide-y divide-subtle/60">
                {cashPosition?.configured && cashPosition.accounts.length > 0 ? (
                  cashPosition.accounts.map((acct) => {
                    const balanceNum = parseFloat(acct.balance);
                    const isCredit = balanceNum >= 0;
                    return (
                      <li key={acct.id} className="flex items-center justify-between px-5 py-2.5">
                        <div className="min-w-0 flex-1">
                          <div className="truncate text-sm text-ink-secondary">{acct.name}</div>
                          <div className="text-[10px] uppercase tracking-wider text-ink-tertiary">
                            {acct.org_name}
                          </div>
                        </div>
                        <span
                          className={cn(
                            "shrink-0 font-mono text-sm tabular-nums",
                            isCredit ? "text-credit" : "text-debit"
                          )}
                        >
                          {formatUSD(acct.balance)}
                        </span>
                      </li>
                    );
                  })
                ) : (
                  <li className="px-5 py-6 text-center text-xs text-ink-tertiary">
                    No bank accounts connected yet.{" "}
                    <Link href="/connectors" className="text-ink-secondary underline-offset-2 hover:text-ink-primary hover:underline">
                      Connect SimpleFIN
                    </Link>{" "}
                    to see live balances.
                  </li>
                )}
              </ul>
            </Card>

            <Card padded={false}>
              <div className="flex items-center gap-2 border-b border-subtle px-5 py-3">
                <span className="h-1.5 w-1.5 rounded-full bg-ink-disabled" />
                <h2 className="text-sm font-semibold">Compliance watch</h2>
                <Link
                  href="/specialty-rules"
                  className="ml-auto text-[11px] text-ink-secondary hover:text-ink-primary"
                >
                  Review rules <ArrowUpRight size={11} className="inline" />
                </Link>
              </div>
              <div className="px-5 py-6 text-center text-xs text-ink-tertiary">
                <p>No active rules yet.</p>
                <p className="mt-1">
                  Add a 1099-NEC trigger, sales-tax nexus tracker, FERS earned-income watch,
                  or any other rule on{" "}
                  <Link
                    href="/specialty-rules"
                    className="text-ink-secondary underline-offset-2 hover:text-ink-primary hover:underline"
                  >
                    Specialty rules
                  </Link>{" "}
                  to populate this panel.
                </p>
              </div>
            </Card>
          </div>
        </div>

        <div className="mt-8">
          <Card padded={false}>
            <div className="flex items-center justify-between border-b border-subtle px-6 py-4">
              <div>
                <h2 className="text-base font-semibold">Agent decisions · transparency</h2>
                <p className="mt-0.5 text-xs text-ink-tertiary">
                  Every agent action will expose its reasoning, source, and rule version here once
                  Phase 2 agents are wired (Brigade orchestration + Qwen3-VL receipt pipeline).
                </p>
              </div>
              <StatusChip tone="ink">Phase 2</StatusChip>
            </div>
            <div className="px-6 py-12 text-center text-sm text-ink-tertiary">
              <p>No agent decisions yet.</p>
              <p className="mt-2 text-xs">
                The pattern is in place. The first decisions will land when the Bookkeeper agent
                begins categorizing your live transactions.
              </p>
            </div>
          </Card>
        </div>
      </PageShell>
    </AppShell>
  );
}
