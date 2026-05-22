import Link from "next/link";
import { ArrowRight, ArrowUpRight, TrendingDown, TrendingUp } from "lucide-react";
import { AppShell } from "@/components/app-shell";
import { EntitySelector } from "@/components/entity-selector";
import { Card, KpiCard, MaterialityBadge, PageShell, SectionHeader, StatusChip, ConfidenceDot } from "@/components/primitives";
import { TransparencyPanel } from "@/components/transparency-panel";
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

export default function DashboardPage() {
  return (
    <AppShell>
      <PageShell>
        <SectionHeader
          title="Dashboard"
          subtitle="Cash position, agent activity, and approvals across all your entities."
          action={<EntitySelector />}
        />

        <div className="grid grid-cols-1 gap-4 md:grid-cols-2 lg:grid-cols-4">
          <KpiCard label="Cash position" value="$487,193" delta="+$18.2k this week" deltaTone="credit" />
          <KpiCard label="Net burn (30d)" value="$24,180" delta="−7% vs prior month" deltaTone="credit" />
          <KpiCard label="Runway" value="14.2 mo" delta="at current burn" deltaTone="ink" />
          <KpiCard label="Awaiting approval" value="3" delta="2 material · 1 specialty" deltaTone="pending" />
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
              <table className="w-full text-sm">
                <thead className="text-[10px] uppercase tracking-wider text-ink-tertiary">
                  <tr className="border-b border-subtle">
                    <th className="px-6 py-2.5 text-left font-medium">Date</th>
                    <th className="px-2 py-2.5 text-left font-medium">Payee · Category</th>
                    <th className="px-2 py-2.5 text-left font-medium">Status</th>
                    <th className="px-2 py-2.5 text-center font-medium">M</th>
                    <th className="px-2 py-2.5 text-center font-medium">Conf</th>
                    <th className="px-6 py-2.5 text-right font-medium">Amount</th>
                  </tr>
                </thead>
                <tbody>
                  {RECENT_TXNS.map((t) => (
                    <tr
                      key={t.id}
                      className="group cursor-pointer border-b border-subtle/60 last:border-b-0 hover:bg-white/[0.02]"
                    >
                      <td className="px-6 py-3 font-mono text-xs text-ink-tertiary whitespace-nowrap">{t.date}</td>
                      <td className="px-2 py-3">
                        <div className="font-medium">{t.payee}</div>
                        <div className="text-xs text-ink-tertiary">{t.category}</div>
                      </td>
                      <td className="px-2 py-3">
                        {t.recon === "reconciled" ? (
                          <StatusChip tone="credit">Reconciled</StatusChip>
                        ) : (
                          <StatusChip tone="pending">Needs review</StatusChip>
                        )}
                      </td>
                      <td className="px-2 py-3 text-center">
                        <MaterialityBadge level={t.materiality} />
                      </td>
                      <td className="px-2 py-3 text-center">
                        <ConfidenceDot value={t.conf} />
                      </td>
                      <td
                        className={cn(
                          "px-6 py-3 text-right font-mono tabular-nums",
                          t.tone === "credit" ? "text-credit" : "text-ink-primary"
                        )}
                      >
                        {t.amount}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </Card>
          </div>

          <div className="space-y-4">
            <Card padded={false}>
              <div className="border-b border-subtle px-5 py-4">
                <h2 className="text-base font-semibold">Cash by account</h2>
                <p className="mt-0.5 text-xs text-ink-tertiary">Real-time balances</p>
              </div>
              <ul className="divide-y divide-subtle/60">
                {[
                  ["Mercury · Operating", "$312,847", "credit" as const],
                  ["Mercury · Tax reserve", "$48,200", "credit" as const],
                  ["Mercury · Vault", "$112,946", "credit" as const],
                  ["Stripe · USD balance", "$8,420", "credit" as const],
                  ["PayPal · USD", "$1,082", "credit" as const],
                  ["Wise · Multi-currency", "$3,698", "credit" as const]
                ].map(([name, amt]) => (
                  <li key={name as string} className="flex items-center justify-between px-5 py-2.5">
                    <span className="text-sm text-ink-secondary">{name}</span>
                    <span className="font-mono text-sm tabular-nums">{amt}</span>
                  </li>
                ))}
              </ul>
            </Card>

            <Card padded={false} className="border-pending/40 bg-pending/[0.04]">
              <div className="flex items-center gap-2 border-b border-pending/30 px-5 py-3">
                <span className="h-1.5 w-1.5 rounded-full bg-pending shadow-[0_0_6px_hsl(43_96%_56%_/_0.8)]" />
                <h2 className="text-sm font-semibold">Compliance watch</h2>
                <Link
                  href="/specialty-rules"
                  className="ml-auto text-[11px] text-ink-secondary hover:text-ink-primary"
                >
                  Review rules <ArrowUpRight size={11} className="inline" />
                </Link>
              </div>
              <ul className="space-y-3 px-5 py-4 text-xs">
                <li className="flex items-start gap-2">
                  <TrendingUp size={14} className="mt-0.5 shrink-0 text-pending" />
                  <span className="text-ink-secondary">
                    <strong className="text-ink-primary">1099-NEC threshold</strong> — Daniel Tran cumulative $487 / $600. Two more invoices triggers reporting.
                  </span>
                </li>
                <li className="flex items-start gap-2">
                  <TrendingDown size={14} className="mt-0.5 shrink-0 text-credit" />
                  <span className="text-ink-secondary">
                    <strong className="text-ink-primary">Sales tax nexus</strong> — TX trailing 12mo $312k / $500k. No action needed.
                  </span>
                </li>
                <li className="flex items-start gap-2">
                  <TrendingUp size={14} className="mt-0.5 shrink-0 text-pending" />
                  <span className="text-ink-secondary">
                    <strong className="text-ink-primary">Q2 estimated tax</strong> — Due June 16. Projected $11,200 based on YTD income.
                  </span>
                </li>
              </ul>
            </Card>
          </div>
        </div>

        <div className="mt-8">
          <Card padded={false}>
            <div className="flex items-center justify-between border-b border-subtle px-6 py-4">
              <div>
                <h2 className="text-base font-semibold">Latest agent decision · transparency</h2>
                <p className="mt-0.5 text-xs text-ink-tertiary">Every agent action exposes its reasoning, source, and rule version.</p>
              </div>
              <StatusChip tone="agent">bookkeeper</StatusChip>
            </div>
            <TransparencyPanel />
          </Card>
        </div>
      </PageShell>
    </AppShell>
  );
}
