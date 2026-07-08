import { getActiveContext } from "@/lib/active-context";
import { AppShell } from "@/components/app-shell";
import { PageShell, SectionHeader } from "@/components/primitives";
import { InvoiceCreateForm } from "./invoice-create-form";

export default async function NewInvoicePage() {
  const ctx = await getActiveContext();
  if (!ctx.activeWorkspace) {
    return (
      <AppShell>
        <PageShell>
          <p className="text-sm text-ink-tertiary">Sign in to create invoices.</p>
        </PageShell>
      </AppShell>
    );
  }

  const initialEntity =
    (ctx.activeEntity && !ctx.isConsolidated ? ctx.activeEntity.id : null) ??
    ctx.entities[0]?.id ??
    "";

  return (
    <AppShell>
      <PageShell>
        <SectionHeader
          title="New invoice"
          subtitle="Creates an AR invoice and posts DR Accounts Receivable / CR Revenue (+ Sales Tax Payable when taxed) to the hash-chained ledger."
        />
        <InvoiceCreateForm
          workspaceId={ctx.activeWorkspace.id}
          entities={ctx.entities.map((e) => ({
            id: e.id,
            display_name: e.display_name
          }))}
          initialEntityId={initialEntity}
        />
      </PageShell>
    </AppShell>
  );
}
