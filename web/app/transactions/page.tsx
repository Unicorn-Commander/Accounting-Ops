import { Filter, Search } from "lucide-react";
import { AppShell } from "@/components/app-shell";
import { EntitySelector } from "@/components/entity-selector";
import {
  Card,
  ConfidenceDot,
  MaterialityBadge,
  PageShell,
  SectionHeader,
  StatusChip
} from "@/components/primitives";
import { getActiveContext } from "@/lib/active-context";
import { cn } from "@/lib/cn";

const TXNS = [
  { date: "May 21", payee: "Stripe payout · Listing-Ops batch 4f2c", category: "Revenue · Marketplace", entity: "Magic Unicorn Inc", amount: "+$12,408.22", tone: "credit", conf: 0.97, materiality: "M" as const, status: "Reconciled", tag: "credit" as const },
  { date: "May 21", payee: "Anthropic, PBC", category: "SaaS · AI", entity: "Magic Unicorn Inc", amount: "-$200.00", tone: "ink", conf: 0.93, materiality: "L" as const, status: "Auto-posted", tag: "credit" as const },
  { date: "May 21", payee: "Hostinger International", category: "Infrastructure · VPS", entity: "Magic Unicorn Inc", amount: "-$8.99", tone: "ink", conf: 0.95, materiality: "L" as const, status: "Auto-posted", tag: "credit" as const },
  { date: "May 20", payee: "Stripe payout · Majiks Studio", category: "Revenue · Music", entity: "Majiks JV", amount: "+$1,082.40", tone: "credit", conf: 0.99, materiality: "L" as const, status: "Reconciled", tag: "credit" as const },
  { date: "May 20", payee: "Cisco Meraki licensing", category: "Infrastructure · network", entity: "Magic Unicorn Inc", amount: "-$420.00", tone: "ink", conf: 0.71, materiality: "L" as const, status: "Needs review", tag: "pending" as const },
  { date: "May 19", payee: "Mercury · ACH transfer", category: "Inter-entity transfer", entity: "Magic Unicorn Inc", amount: "+$5,000.00", tone: "credit", conf: 0.62, materiality: "M" as const, status: "Needs review", tag: "pending" as const },
  { date: "May 19", payee: "Postmark Outbound", category: "SaaS · Email", entity: "Magic Unicorn Inc", amount: "-$15.00", tone: "ink", conf: 0.98, materiality: "L" as const, status: "Auto-posted", tag: "credit" as const },
  { date: "May 18", payee: "Daniel Tran · INV-08", category: "Contractor · 1099-NEC", entity: "Magic Unicorn Inc", amount: "-$420.00", tone: "ink", conf: 0.99, materiality: "L" as const, status: "Reconciled", tag: "credit" as const },
  { date: "May 18", payee: "Wise · USD→USDC payout", category: "International · contributor", entity: "Majiks JV", amount: "-$612.55", tone: "ink", conf: 0.85, materiality: "L" as const, status: "Reconciled", tag: "credit" as const },
  { date: "May 17", payee: "Anthropic · Claude API overflow", category: "Compute · LLM", entity: "Magic Unicorn Inc", amount: "-$48.20", tone: "ink", conf: 0.96, materiality: "L" as const, status: "Auto-posted", tag: "credit" as const },
  { date: "May 17", payee: "AWS · S3 + CloudFront", category: "Infrastructure · CDN", entity: "Magic Unicorn Inc", amount: "-$87.42", tone: "ink", conf: 0.94, materiality: "L" as const, status: "Auto-posted", tag: "credit" as const },
  { date: "May 16", payee: "eBay Finances payout · batch 19c2", category: "Revenue · Listing-Ops", entity: "Magic Unicorn Inc", amount: "+$1,847.81", tone: "credit", conf: 0.92, materiality: "M" as const, status: "Reconciled", tag: "credit" as const }
];

export default async function TransactionsPage() {
  const ctx = await getActiveContext();
  const canConsolidate = ctx.activeWorkspace
    ? ["owner", "admin"].includes(ctx.activeWorkspace.role)
    : false;
  return (
    <AppShell>
      <PageShell>
        <SectionHeader
          title="Transactions"
          subtitle="Every money event across all connected accounts."
        />

        <Card padded={false}>
          <div className="flex flex-wrap items-center gap-2 border-b border-subtle px-5 py-3">
            <div className="relative min-w-[240px] flex-1">
              <Search size={14} className="pointer-events-none absolute left-3 top-1/2 -translate-y-1/2 text-ink-tertiary" />
              <input
                type="text"
                placeholder="Search payee, category, amount, or hash..."
                className="w-full rounded-md border border-default bg-canvas py-1.5 pl-9 pr-3 text-sm text-ink-primary placeholder:text-ink-disabled focus:border-brand-500 focus:outline-none"
              />
            </div>
            <button className="inline-flex items-center gap-1.5 rounded-md border border-default bg-surface px-3 py-1.5 text-xs text-ink-secondary hover:bg-elevated">
              <Filter size={13} /> Filters
            </button>
            <select className="rounded-md border border-default bg-surface px-3 py-1.5 text-xs">
              <option>Last 30 days</option>
              <option>Last 90 days</option>
              <option>This quarter</option>
              <option>This year</option>
              <option>Custom range</option>
            </select>
            <EntitySelector
              entities={ctx.entities}
              activeEntityId={ctx.isConsolidated ? "consolidated" : ctx.activeEntity?.id}
              canConsolidate={canConsolidate}
              size="sm"
            />
            <span className="ml-auto text-[11px] text-ink-tertiary">
              {TXNS.length} of 1,847 transactions
            </span>
          </div>

          <table className="w-full text-sm">
            <thead className="text-[10px] uppercase tracking-wider text-ink-tertiary">
              <tr className="border-b border-subtle">
                <th className="px-5 py-2.5 text-left font-medium">Date</th>
                <th className="px-2 py-2.5 text-left font-medium">Payee · Category · Entity</th>
                <th className="px-2 py-2.5 text-left font-medium">Status</th>
                <th className="px-2 py-2.5 text-center font-medium">M</th>
                <th className="px-2 py-2.5 text-center font-medium">Conf</th>
                <th className="px-5 py-2.5 text-right font-medium">Amount</th>
              </tr>
            </thead>
            <tbody>
              {TXNS.map((t, i) => (
                <tr
                  key={i}
                  className="group cursor-pointer border-b border-subtle/60 last:border-b-0 hover:bg-white/[0.02]"
                >
                  <td className="whitespace-nowrap px-5 py-3 font-mono text-xs text-ink-tertiary">{t.date}</td>
                  <td className="px-2 py-3">
                    <div className="font-medium">{t.payee}</div>
                    <div className="text-xs text-ink-tertiary">
                      {t.category} · {t.entity}
                    </div>
                  </td>
                  <td className="px-2 py-3">
                    <StatusChip tone={t.tag}>{t.status}</StatusChip>
                  </td>
                  <td className="px-2 py-3 text-center">
                    <MaterialityBadge level={t.materiality} />
                  </td>
                  <td className="px-2 py-3 text-center">
                    <ConfidenceDot value={t.conf} />
                  </td>
                  <td
                    className={cn(
                      "px-5 py-3 text-right font-mono tabular-nums",
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

        <p className="mt-4 text-xs text-ink-tertiary">
          Demo data. Phase 1 wires real connectors: eBay Finances MCP, Plaid Transactions, Stripe, PayPal, Mercury, Wise.
        </p>
      </PageShell>
    </AppShell>
  );
}
