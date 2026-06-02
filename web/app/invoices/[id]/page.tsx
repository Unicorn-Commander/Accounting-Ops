import Link from "next/link";
import { notFound } from "next/navigation";
import { ArrowLeft, Download, Hash } from "lucide-react";
import { AppShell } from "@/components/app-shell";
import { Card, PageShell, SectionHeader, StatusChip } from "@/components/primitives";
import { getActiveContext } from "@/lib/active-context";
import {
  formatMoney,
  getInvoiceDetail,
  statusTone,
  type InvoiceDetail
} from "@/lib/invoices";
import { shortDate } from "@/lib/transactions";
import { InvoiceActions } from "./invoice-actions";

export default async function InvoiceDetailPage(props: {
  params: Promise<{ id: string }>;
}) {
  const { id } = await props.params;
  const ctx = await getActiveContext();
  const workspace = ctx.activeWorkspace;
  if (!workspace) {
    return (
      <AppShell>
        <PageShell>
          <p className="text-sm text-ink-tertiary">Sign in to view this invoice.</p>
        </PageShell>
      </AppShell>
    );
  }

  const inv: InvoiceDetail | null = await getInvoiceDetail(workspace.id, id);
  if (!inv) notFound();

  const canWrite = ["owner", "admin", "accountant", "bookkeeper", "cpa"].includes(
    workspace.role
  );
  const pdfHref = `/api/workspaces/${workspace.id}/invoices/${inv.id}/pdf`;

  return (
    <AppShell>
      <PageShell>
        <div className="mb-4">
          <Link
            href="/invoices"
            className="inline-flex items-center gap-1 text-xs text-ink-tertiary hover:text-ink-secondary"
          >
            <ArrowLeft size={12} /> Back to invoices
          </Link>
        </div>

        <SectionHeader
          title={inv.invoice_number}
          subtitle={`${inv.entity_display_name ?? ""} → ${inv.customer_name ?? "—"}`}
          action={
            <div className="flex items-center gap-2">
              <StatusChip tone={statusTone(inv.status)}>{inv.status}</StatusChip>
              <a
                href={pdfHref}
                target="_blank"
                rel="noopener noreferrer"
                className="inline-flex items-center gap-1.5 rounded-md border border-default bg-surface px-3 py-1.5 text-xs font-medium text-ink-secondary hover:bg-elevated"
              >
                <Download size={13} /> PDF
              </a>
            </div>
          }
        />

        <div className="grid grid-cols-1 gap-4 lg:grid-cols-3">
          {/* Main: lines + payments */}
          <div className="space-y-4 lg:col-span-2">
            <Card padded={false}>
              <div className="border-b border-subtle px-5 py-3">
                <p className="text-[10px] font-medium uppercase tracking-wider text-ink-tertiary">
                  Line items
                </p>
              </div>
              <div className="overflow-x-auto">
                <table className="w-full min-w-[480px] text-sm">
                  <thead className="text-[10px] uppercase tracking-wider text-ink-tertiary">
                    <tr className="border-b border-subtle">
                      <th className="px-5 py-2 text-left font-medium">Description</th>
                      <th className="px-2 py-2 text-right font-medium">Qty</th>
                      <th className="px-2 py-2 text-right font-medium">Unit</th>
                      <th className="px-2 py-2 text-right font-medium">Tax</th>
                      <th className="px-5 py-2 text-right font-medium">Amount</th>
                    </tr>
                  </thead>
                  <tbody>
                    {inv.lines.map((l) => (
                      <tr key={l.id} className="border-b border-subtle/60 last:border-b-0">
                        <td className="px-5 py-2.5">
                          <div>{l.description}</div>
                          {l.revenue_account_canonical && (
                            <div className="text-[10px] font-mono text-ink-tertiary">
                              {l.revenue_account_canonical}
                            </div>
                          )}
                        </td>
                        <td className="px-2 py-2.5 text-right font-mono tabular-nums text-ink-secondary">
                          {l.quantity}
                        </td>
                        <td className="px-2 py-2.5 text-right font-mono tabular-nums text-ink-secondary">
                          {formatMoney(l.unit_price, inv.currency)}
                        </td>
                        <td className="px-2 py-2.5 text-right font-mono tabular-nums text-ink-tertiary">
                          {parseFloat(l.tax_amount) > 0
                            ? formatMoney(l.tax_amount, inv.currency)
                            : "—"}
                        </td>
                        <td className="px-5 py-2.5 text-right font-mono tabular-nums text-ink-primary">
                          {formatMoney(l.amount, inv.currency)}
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
              {/* Totals */}
              <div className="flex flex-col items-end gap-1 border-t border-subtle px-5 py-3 text-sm">
                <TotalRow label="Subtotal" value={formatMoney(inv.subtotal, inv.currency)} />
                {parseFloat(inv.discount_total) > 0 && (
                  <TotalRow
                    label="Discount"
                    value={"-" + formatMoney(inv.discount_total, inv.currency)}
                  />
                )}
                {parseFloat(inv.tax_total) > 0 && (
                  <TotalRow label="Sales tax" value={formatMoney(inv.tax_total, inv.currency)} />
                )}
                <TotalRow label="Total" value={formatMoney(inv.total, inv.currency)} bold />
                <TotalRow
                  label="Balance due"
                  value={formatMoney(inv.balance_due, inv.currency)}
                  bold
                />
              </div>
            </Card>

            <Card padded={false}>
              <div className="border-b border-subtle px-5 py-3">
                <p className="text-[10px] font-medium uppercase tracking-wider text-ink-tertiary">
                  Payments
                </p>
              </div>
              {inv.payments.length === 0 ? (
                <p className="px-5 py-6 text-center text-xs text-ink-tertiary">
                  No payments recorded yet.
                </p>
              ) : (
                <table className="w-full text-sm">
                  <tbody>
                    {inv.payments.map((p) => (
                      <tr key={p.id} className="border-b border-subtle/60 last:border-b-0">
                        <td className="px-5 py-2.5 font-mono text-xs text-ink-tertiary">
                          {shortDate(p.received_date)}
                        </td>
                        <td className="px-2 py-2.5 text-xs">{p.method}</td>
                        <td className="px-2 py-2.5 text-xs text-ink-tertiary">
                          {p.memo ?? ""}
                        </td>
                        <td className="px-5 py-2.5 text-right font-mono tabular-nums text-credit">
                          {formatMoney(p.amount, inv.currency)}
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              )}
            </Card>
          </div>

          {/* Side rail: meta + actions */}
          <div className="space-y-4">
            <Card>
              <dl className="space-y-2 text-xs">
                <Meta label="Issue date" value={shortDate(inv.issue_date)} />
                <Meta label="Due date" value={shortDate(inv.due_date)} />
                <Meta label="Customer" value={inv.customer_name ?? "—"} />
                {inv.po_number && <Meta label="PO number" value={inv.po_number} />}
                <Meta label="Source" value={inv.external_source} />
                {inv.external_ref && <Meta label="External ref" value={inv.external_ref} mono />}
                {inv.journal_entry_id && (
                  <div className="flex items-center justify-between gap-2">
                    <dt className="text-ink-tertiary">Journal entry</dt>
                    <dd className="flex items-center gap-1 font-mono text-[10px] text-ink-secondary">
                      <Hash size={10} />
                      <Link
                        href={`/journal/${inv.journal_entry_id}`}
                        className="hover:text-ink-primary"
                      >
                        {inv.journal_entry_id.slice(0, 8)}
                      </Link>
                    </dd>
                  </div>
                )}
                {inv.void_reason && (
                  <Meta label="Void reason" value={inv.void_reason} />
                )}
              </dl>
              {inv.notes && (
                <p className="mt-3 border-t border-subtle pt-3 text-xs text-ink-tertiary">
                  {inv.notes}
                </p>
              )}
            </Card>

            {canWrite && (
              <InvoiceActions
                workspaceId={workspace.id}
                invoiceId={inv.id}
                status={inv.status}
                balanceDue={inv.balance_due}
                currency={inv.currency}
                hasPayments={inv.payments.length > 0}
              />
            )}
          </div>
        </div>
      </PageShell>
    </AppShell>
  );
}

function TotalRow({
  label,
  value,
  bold
}: {
  label: string;
  value: string;
  bold?: boolean;
}) {
  return (
    <div className="flex w-52 items-center justify-between">
      <span className={bold ? "font-semibold text-ink-primary" : "text-ink-tertiary"}>
        {label}
      </span>
      <span
        className={
          bold
            ? "font-mono font-semibold tabular-nums text-ink-primary"
            : "font-mono tabular-nums text-ink-secondary"
        }
      >
        {value}
      </span>
    </div>
  );
}

function Meta({
  label,
  value,
  mono
}: {
  label: string;
  value: string;
  mono?: boolean;
}) {
  return (
    <div className="flex items-center justify-between gap-2">
      <dt className="text-ink-tertiary">{label}</dt>
      <dd
        className={
          mono
            ? "max-w-[160px] truncate font-mono text-[10px] text-ink-secondary"
            : "max-w-[160px] truncate text-ink-secondary"
        }
        title={value}
      >
        {value}
      </dd>
    </div>
  );
}
