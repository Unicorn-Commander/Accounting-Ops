import Link from "next/link";
import { FileText, Shield } from "lucide-react";
import { AppShell } from "@/components/app-shell";
import { EntitySelector } from "@/components/entity-selector";
import {
  Card,
  EmptyState,
  PageShell,
  SectionHeader,
  StatusChip
} from "@/components/primitives";
import { getActiveContext } from "@/lib/active-context";
import {
  agingTone,
  formatMoney,
  getInvoices,
  statusTone,
  type InvoiceListRow
} from "@/lib/invoices";
import { shortDate } from "@/lib/transactions";
import { cn } from "@/lib/cn";

type SearchParams = { status?: string };

export default async function InvoicesPage(props: {
  searchParams: Promise<SearchParams>;
}) {
  const sp = await props.searchParams;
  const ctx = await getActiveContext();
  const workspace = ctx.activeWorkspace;
  const entityFilter =
    ctx.activeEntity?.id && !ctx.isConsolidated ? ctx.activeEntity.id : undefined;
  const canConsolidate = ctx.activeWorkspace
    ? ["owner", "admin"].includes(ctx.activeWorkspace.role)
    : false;

  const data = workspace
    ? await getInvoices(workspace.id, {
        entityId: entityFilter,
        status: sp.status,
        limit: 200
      })
    : null;

  const invoices: InvoiceListRow[] = data?.invoices ?? [];
  const total = data?.total ?? 0;

  // Summary: outstanding (sum of balance_due on non-void), overdue count.
  let outstanding = 0;
  let overdueCount = 0;
  let overdueAmount = 0;
  for (const inv of invoices) {
    if (inv.status === "void") continue;
    const bal = parseFloat(inv.balance_due);
    outstanding += bal;
    if (bal > 0 && (inv.aging === "31-60" || inv.aging === "61-90" || inv.aging === "90+" || inv.aging === "1-30")) {
      overdueCount += 1;
      overdueAmount += bal;
    }
  }

  const STATUS_TABS: { label: string; value: string }[] = [
    { label: "All", value: "" },
    { label: "Draft", value: "draft" },
    { label: "Sent", value: "sent" },
    { label: "Partial", value: "partial" },
    { label: "Paid", value: "paid" },
    { label: "Void", value: "void" }
  ];

  return (
    <AppShell>
      <PageShell>
        <SectionHeader
          title="Invoices"
          subtitle={
            workspace
              ? `${total} ${total === 1 ? "invoice" : "invoices"} · accounts receivable`
              : "Sign in to view invoices."
          }
          action={
            <div className="flex items-center gap-2">
              <EntitySelector
                entities={ctx.entities}
                activeEntityId={
                  ctx.isConsolidated ? "consolidated" : ctx.activeEntity?.id
                }
                canConsolidate={canConsolidate}
                size="sm"
              />
              <Link
                href="/invoices/new"
                className="inline-flex items-center gap-1.5 rounded-md bg-brand-500 px-3 py-1.5 text-xs font-medium text-white hover:bg-brand-600"
              >
                + New invoice
              </Link>
            </div>
          }
        />

        {workspace && invoices.length > 0 && (
          <div className="mb-4 grid grid-cols-1 gap-3 sm:grid-cols-3">
            <Card>
              <p className="text-[10px] font-medium uppercase tracking-wider text-ink-tertiary">
                Outstanding (open balance)
              </p>
              <p className="mt-1 font-mono text-lg text-ink-primary tabular-nums">
                {formatMoney(outstanding)}
              </p>
            </Card>
            <Card>
              <p className="text-[10px] font-medium uppercase tracking-wider text-ink-tertiary">
                Past-due amount
              </p>
              <p className="mt-1 font-mono text-lg text-debit tabular-nums">
                {formatMoney(overdueAmount)}
              </p>
            </Card>
            <Card>
              <p className="text-[10px] font-medium uppercase tracking-wider text-ink-tertiary">
                Past-due invoices
              </p>
              <p className="mt-1 font-mono text-lg text-ink-primary tabular-nums">
                {overdueCount}
              </p>
            </Card>
          </div>
        )}

        <Card padded={false}>
          <div className="flex flex-wrap items-center gap-1.5 border-b border-subtle px-5 py-3">
            {STATUS_TABS.map((tab) => {
              const active = (sp.status ?? "") === tab.value;
              return (
                <Link
                  key={tab.value || "all"}
                  href={tab.value ? `/invoices?status=${tab.value}` : "/invoices"}
                  className={cn(
                    "rounded-md px-3 py-1.5 text-xs font-medium",
                    active
                      ? "bg-brand-500 text-white"
                      : "border border-default bg-surface text-ink-secondary hover:bg-elevated"
                  )}
                >
                  {tab.label}
                </Link>
              );
            })}
          </div>

          {invoices.length === 0 ? (
            <div className="px-6 py-10">
              <EmptyState
                variant={sp.status ? "filtered-empty" : "no-data"}
                title={
                  sp.status
                    ? "No invoices match this filter."
                    : "No invoices yet."
                }
                description={
                  sp.status
                    ? "Try a different status filter."
                    : "Create your first invoice — it posts to the hash-chained ledger as DR Accounts Receivable / CR Revenue."
                }
                primaryAction={
                  <Link
                    href="/invoices/new"
                    className="inline-flex items-center gap-1.5 rounded-md bg-brand-500 px-3 py-1.5 text-xs font-medium text-white hover:bg-brand-600"
                  >
                    + New invoice
                  </Link>
                }
                className="border-0 bg-transparent"
              />
            </div>
          ) : (
            <div className="overflow-x-auto">
              <table className="w-full min-w-[760px] text-sm">
                <thead className="text-[10px] uppercase tracking-wider text-ink-tertiary">
                  <tr className="border-b border-subtle">
                    <th className="px-5 py-2.5 text-left font-medium">Invoice</th>
                    <th className="px-2 py-2.5 text-left font-medium">Customer</th>
                    <th className="px-2 py-2.5 text-left font-medium">Issued</th>
                    <th className="px-2 py-2.5 text-left font-medium">Due</th>
                    <th className="px-2 py-2.5 text-center font-medium">Status</th>
                    <th className="px-2 py-2.5 text-center font-medium">Aging</th>
                    <th className="px-2 py-2.5 text-right font-medium">Total</th>
                    <th className="px-5 py-2.5 text-right font-medium">Balance</th>
                  </tr>
                </thead>
                <tbody>
                  {invoices.map((inv) => (
                    <tr
                      key={inv.id}
                      className={cn(
                        "border-b border-subtle/60 last:border-b-0 hover:bg-white/[0.02]",
                        inv.status === "void" && "opacity-50"
                      )}
                    >
                      <td className="whitespace-nowrap px-5 py-3 font-mono text-xs">
                        <Link
                          href={`/invoices/${inv.id}`}
                          className="text-brand-300 hover:text-brand-200"
                        >
                          {inv.invoice_number}
                        </Link>
                        {inv.entity_display_name && (
                          <div className="text-[10px] text-ink-tertiary">
                            {inv.entity_display_name}
                          </div>
                        )}
                      </td>
                      <td className="px-2 py-3">
                        <div className="max-w-[180px] truncate">
                          {inv.customer_name ?? "—"}
                        </div>
                        {inv.po_number && (
                          <div className="text-[10px] text-ink-tertiary">
                            PO {inv.po_number}
                          </div>
                        )}
                      </td>
                      <td className="whitespace-nowrap px-2 py-3 font-mono text-xs text-ink-tertiary">
                        {shortDate(inv.issue_date)}
                      </td>
                      <td className="whitespace-nowrap px-2 py-3 font-mono text-xs text-ink-tertiary">
                        {shortDate(inv.due_date)}
                      </td>
                      <td className="px-2 py-3 text-center">
                        <StatusChip tone={statusTone(inv.status)}>
                          {inv.status}
                        </StatusChip>
                      </td>
                      <td className="px-2 py-3 text-center">
                        {inv.status !== "void" && inv.aging !== "paid" ? (
                          <StatusChip tone={agingTone(inv.aging)}>
                            {inv.aging}
                          </StatusChip>
                        ) : (
                          <span className="text-[10px] text-ink-tertiary">—</span>
                        )}
                      </td>
                      <td className="px-2 py-3 text-right font-mono tabular-nums text-ink-secondary">
                        {formatMoney(inv.total, inv.currency)}
                      </td>
                      <td className="px-5 py-3 text-right font-mono tabular-nums text-ink-primary">
                        {formatMoney(inv.balance_due, inv.currency)}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </Card>

        <p className="mt-4 flex items-center gap-2 text-[11px] text-ink-tertiary">
          <Shield size={11} />
          Every invoice posts double-entry to the per-entity hash chain. Voids
          create reversing entries — the ledger is never rewritten.
        </p>
      </PageShell>
    </AppShell>
  );
}
